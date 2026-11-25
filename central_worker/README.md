# 중앙 수집 워커

시설 브리지가 중앙 MQTT로 올린 telemetry / event / ack를 받아 중앙 MySQL(`coldguard_central`)에 저장합니다. 중앙 EKS에서 실행합니다.

<br />

## 동작

1. 중앙 Mosquitto를 공유 구독(`$share/central-worker/coldguard/+/+/{telemetry,event,ack}`)으로 구독합니다.
2. 시설 프로그램과 같은 `common` 규칙으로 메시지를 검사합니다.
3. 검사를 통과하면 테이블에 저장합니다.
   - `telemetry` → `telemetry`
   - `event` → `event_log`
   - `ack` → `command_ack` (ack에는 site_id·device_id가 없어 토픽에서 채웁니다)
4. 저장했거나 같은 데이터가 이미 있음을 확인한 뒤에만 MQTT 수신 확인(PUBACK)을 보냅니다.

<br />

## 데이터 보장

- **손실 없음**: 고정 클라이언트 ID와 영속 세션(`clean_session=False`)을 사용합니다. 워커가 꺼져 있거나 저장 전에 종료되어도 중앙 브로커가 메시지를 보관했다가 다시 전달합니다.
- **중복 없음**: 테이블의 유니크 키로 같은 데이터는 한 번만 저장합니다. 재전달된 메시지는 `duplicate`로 처리합니다.
- **충돌 감지**: 같은 키인데 내용이 다르면 저장하지 않고 `duplicate_conflict` 로그를 남깁니다. 시뮬레이터 `SIM_DB` 초기화로 seq가 1부터 다시 시작된 경우 등이 해당합니다.
- **잘못된 메시지**: 형식이 틀린 메시지는 `message_rejected` 로그를 남기고 버립니다. 같은 메시지를 계속 재시도하지 않습니다.
- **DB 장애**: 저장에 실패하면 메시지를 확인하지 않고 2초마다 다시 시도합니다. DB가 돌아오면 이어서 처리합니다.

<br />

## 설정

환경변수로 지정합니다. 예시는 [`config/central_worker.env.example`](../config/central_worker.env.example)을 참고하세요.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `mosquitto` / `1883` | 중앙 Mosquitto (클러스터 내부 평문 리스너) |
| `MQTT_USER` / `MQTT_PASS` | `central-worker` / (필수) | MQTT 계정 |
| `MQTT_CLIENT_ID` | `central-worker` | 영속 세션 식별자. 바꾸면 보관 중인 메시지를 받지 못합니다 |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_DB` | `mysql` / `3306` / `coldguard_central` | 중앙 MySQL |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `coldguard_worker` / (필수) | DB 계정 (SELECT 전체, 수집 테이블 3개 INSERT) |
| `METRICS_PORT` | `9203` | `/metrics` 포트 |
| `FUTURE_SKEW_SEC` | `300` | 현재보다 이 초 이상 미래인 시각은 거부 |

<br />

## 실행

공용 이미지에 포함되어 있으며 실행 명령으로 워커를 고릅니다.

```bash
python -m central_worker.central_worker
```

배포 매니페스트는 비공개 저장소 `coldguard-deploy`의 `central-worker/`에 있습니다.

<br />

## 지표

| 이름 | 설명 |
|---|---|
| `coldguard_central_messages_total{channel,result}` | 처리 건수 (`stored` / `duplicate` / `rejected`) |
| `coldguard_central_db_up` | 마지막 DB 처리 성공 여부 |
| `coldguard_central_mqtt_up` | MQTT 연결 여부 |

<br />

## 시험

```bash
python -m unittest tests.test_central_worker -v
```

MQTT·MySQL에 연결하지 않고 검사·저장 규칙만 시험합니다.
