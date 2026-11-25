# 통제 API (중앙 허브)

관제 UI·관리자 CLI의 온도 변경 명령을 받아 중앙 Mosquitto로 시설에 내려보내고, 시설의 판정 결과와 장비 상태를 조회한다.

## 동작
1. 명령 요청: 키 확인 → 형식 검사 → 중앙 MQTT 연결 확인 → `command_request` 기록 → `coldguard/<site>/<device>/cmd` 발행(QoS 1, 브로커 수신 확인 대기) → 202 응답
2. 시설 controller가 판정(미등록 장비 → 중복 → 만료 → 현장 잠금 → 허용 온도 순서)하고 ack를 올린다.
3. 수집 워커가 ack를 `command_ack`에 저장하고, API는 이것을 읽어 결과를 보여준다.

- API는 인증·권한과 형식만 검사한다. 허용 온도(-50 ≤ 값 < -10)·만료·중복·잠금 판정은 시설 controller가 한다(엣지 최종 검증).
- 명령 유효시간은 발급 시각부터 60초(`CMD_TTL_SEC`). 늦게 도착한 명령은 시설에서 expired로 거부된다.
- API는 명령을 다시 보내지 않는다. 시설 연결이 끊긴 동안의 조치는 시설 현장 웹이 맡는다.
- ack를 MQTT로 직접 구독하지 않는다(재시작 중 유실, 파드마다 중복 수신을 피하려고 DB를 읽는다).

## 엔드포인트
인증: `Authorization: Bearer <API 키>`. 시각은 모두 UTC `Z`(밀리초).

| 메서드·경로 | 권한 | 설명 |
|---|---|---|
| `GET /healthz` | 없음 | 프로세스 생존(liveness) |
| `GET /readyz` | 없음 | DB 연결 확인(readiness). 응답에 `db`, `mqtt` 참·거짓 |
| `GET /api/v1/me` | read | 키 이름·권한·시설 |
| `POST /api/v1/commands` | command | 온도 변경 명령 |
| `GET /api/v1/commands/{cmd_id}` | read | 명령 하나와 결과 |
| `GET /api/v1/commands?site_id=&device_id=&limit=` | read | 최근 명령(기본 50, 최대 200) |
| `GET /api/v1/devices?site_id=` | read | 장비별 최신 측정값 |
| `GET /api/v1/devices/{site_id}/{device_id}/telemetry?minutes=` | read | 온도 추이(기본 60분, 최대 1440분, 최대 720구간) |
| `GET /api/v1/events?site_id=&device_id=&minutes=&limit=` | read | 경보·차단·잠금 이벤트(기본 1440분·100건, 최대 10080분·500건) |

`site_id`를 빼면 키에 허용된 모든 시설을 조회한다.

### 명령 요청
```json
POST /api/v1/commands
{"site_id": "site01", "device_id": "fz01", "value": -22.0, "operator": "관제01"}
```
- `value`: JSON 숫자, 소수 둘째 자리까지, 절댓값 999.99 이하(저장 형식). 허용 온도 범위 밖이어도 시설로 전달되어 controller가 `out_of_range`로 거부한다.
- `operator`(선택): 30자 이하. 명령 기록의 `issued_by`가 `키이름:운영자`가 된다(없으면 `키이름`).
- 응답 202 (`Location: /api/v1/commands/{cmd_id}`):
```json
{"cmd_id": "6f1c...", "site_id": "site01", "device_id": "fz01", "action": "set_temp", "value": -22.0,
 "issued_by": "kt-ui:관제01", "issued_at": "2026-11-26T01:00:00.123Z", "expires_at": "2026-11-26T01:01:00.123Z",
 "status": "pending", "reason": null, "decided_at": null, "replay_rejected": 0}
```

### 명령 상태
| status | 뜻 |
|---|---|
| `pending` | 응답 대기(만료 전) |
| `accepted` | 시설에서 적용 완료 |
| `rejected` | 시설에서 거부. `reason`: locked, expired, out_of_range, duplicate, unknown_device, apply_timeout |
| `no_response` | 만료됐는데 결과 미수신. 시설 연결이 복구되면 늦게 온 결과(보통 expired)로 바뀐다 |

- 첫 판정이 결과다. 같은 명령이 다시 들어와 `duplicate`로 거부되면 원래 결과는 그대로 두고 `replay_rejected`로 센다.

### 오류
| 코드 | error | 상황 |
|---|---|---|
| 400 | `invalid_json`, `unknown_field`, `missing_field`, `unknown_site`, `invalid_device_id`, `invalid_value`, `invalid_operator`, `invalid_<param>` | 형식 오류(발행하지 않음) |
| 401 | `unauthorized` | 키 없음·틀림 |
| 403 | `forbidden` | 조회 전용 키로 명령, 키에 허용되지 않은 시설 |
| 404 | `not_found` | 없는 명령(또는 키의 시설이 아닌 명령) |
| 413 / 415 | | 본문 4KB 초과 / JSON이 아님 |
| 503 | `mqtt_unavailable` | 중앙 MQTT 끊김. `sent: false`면 발행하지 않음 |
| 503 | `db_unavailable` | DB 오류. 명령은 기록·발행하지 않음 |
| 503 | `publish_unconfirmed` | 기록 후 발행했지만 수신 확인 실패. `cmd_id`로 결과를 조회한다(만료 후에는 적용되지 않음) |

## API 키 파일
`API_KEYS_FILE`(기본 `/etc/control-api/keys.json`, Secret `control-api-keys`)은 JSON 배열이다.
```json
[{"name": "kt-ui", "key": "<32자 이상 무작위>", "scope": "command", "sites": ["site01"]},
 {"name": "kt-load", "key": "<32자 이상 무작위>", "scope": "read", "sites": ["site01"]}]
```
- `name`: 영문 소문자·숫자·하이픈 19자 이하. 명령 기록과 로그에 남는다.
- `scope`: `read`(조회만) 또는 `command`(조회 + 명령). `sites`: 설정 `SITES` 안의 시설.
- 키를 바꾸면 파드를 다시 시작해야 반영된다. 키 값은 로그에 남기지 않는다.

## 설정
`config/control_api.env.example` 참고. 필수: `MQTT_PASS`, `MQTT_CLIENT_ID`(파드마다 다르게), `MYSQL_PASSWORD`, `SITES`.

## 지표 (`METRICS_PORT`, 기본 9204)
- `coldguard_api_requests_total{endpoint,method,code}`, `coldguard_api_request_duration_seconds{endpoint}`
- `coldguard_api_commands_total{result}`: sent, mqtt_unavailable, publish_unconfirmed, db_unavailable, 형식 오류 코드 등
- `coldguard_api_auth_failures_total{reason}`: missing, invalid, scope, site
- `coldguard_api_mqtt_up`, `coldguard_api_db_up`

## 로그 (한 줄 JSON)
`command_sent`(cmd_id, 시설, 장비, 값, issued_by, key), `command_not_sent`, `command_publish_unconfirmed`,
`control_api_auth_rejected`, `control_api_db_error`, `control_api_mqtt_connect`/`disconnect`. 비밀번호와 키는 남기지 않는다.

## 실행
- 배포: gunicorn (coldguard-deploy `control-api/deployment.yaml`)
- 로컬: 환경변수를 정한 뒤 `python -m control_api.app` (Flask 개발 서버, 127.0.0.1:8000)
- 시험: `python -m unittest tests.test_control_api -v` (실제 MQTT·MySQL에 연결하지 않음)
