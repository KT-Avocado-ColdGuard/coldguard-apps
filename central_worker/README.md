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

## 여러 개로 실행 (HPA 2~4)

StatefulSet으로 배포하고 HPA가 CPU 사용률에 따라 2~4개로 조절합니다. 공유 구독이라 메시지는 파드끼리 나눠 받습니다.

- 파드 이름(`central-worker-0`, `-1`, ...)을 MQTT 클라이언트 ID로 씁니다. 파드마다 다르고, 재시작해도 같은 번호로 돌아옵니다.
- **기본 파드**(번호 < `STABLE_REPLICAS`, 기본 0·1번): 종료할 때 구독을 유지합니다. 재시작이나 야간 중지 동안 들어온 메시지는 브로커가 보관했다가 다시 전달합니다.
- **늘어난 파드**(2·3번): HPA가 줄일 때 돌아오지 않을 수 있어서, 종료 신호를 받으면 구독을 해제하고 이미 받은 메시지를 모두 저장한 뒤 끝냅니다(`drain_start` → `drain_done` 로그). 이 파드 몫으로 메시지가 묶이지 않습니다.
- **다 비우지 못한 경우**: DB 장애 등으로 `DRAIN_SEC` 안에 처리하지 못하면 `drain_timeout` 로그를 남기고 종료 코드 1로 끝납니다. 남은 메시지는 그 파드의 세션에 보관되며, 회수 모드로 꺼냅니다.

<br />

## 회수 모드 (`DRAIN_ONLY=true`)

`drain_timeout`으로 끝난 파드의 세션에 남은 메시지를 저장하기 위한 일회성 실행입니다.

- `MQTT_CLIENT_ID`에 회수할 파드 이름(예: `central-worker-3`)을 반드시 지정합니다.
- 접속하면 그 세션의 구독을 해제하고(새 메시지가 더 쌓이지 않게), 브로커가 보관하던 메시지만 저장합니다.
- 3초간 새 메시지가 없으면 `drain_done`으로 끝납니다(종료 코드 0). `DRAIN_SEC` 안에 끝내지 못하면 종료 코드 1입니다.
- 같은 이름의 워커 파드가 떠 있을 때는 실행하지 않습니다. 같은 ID로 동시에 접속하면 서로 연결을 끊습니다.
- 쿠버네티스 Job 파일과 절차는 `coldguard-deploy`의 `ops/recover-central-worker.yaml`에 있습니다.

<br />

## 설정

환경변수로 지정합니다. 예시는 [`config/central_worker.env.example`](../config/central_worker.env.example)을 참고하세요.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `mosquitto` / `1883` | 중앙 Mosquitto (클러스터 내부 평문 리스너) |
| `MQTT_USER` / `MQTT_PASS` | `central-worker` / (필수) | MQTT 계정 |
| `MQTT_CLIENT_ID` | `central-worker` | 영속 세션 식별자. 배포에서는 파드 이름을 넣습니다 |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_DB` | `mysql` / `3306` / `coldguard_central` | 중앙 MySQL |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `coldguard_worker` / (필수) | DB 계정 (SELECT 전체, 수집 테이블 3개 INSERT) |
| `METRICS_PORT` | `9203` | `/metrics` 포트 |
| `FUTURE_SKEW_SEC` | `300` | 현재보다 이 초 이상 미래인 시각은 거부 |
| `STABLE_REPLICAS` | `2` | HPA 최소 개수와 같게 둡니다. 이 번호 미만 파드는 종료 때 구독을 유지합니다 |
| `DRAIN_SEC` | `40` | 메시지를 비우는 최대 시간(초). 넘기면 실패(종료 코드 1) |
| `DRAIN_ONLY` | `false` | `true`면 회수 모드. `MQTT_CLIENT_ID`를 반드시 지정 |

<br />

## 실행

공용 이미지에 포함되어 있으며 실행 명령으로 워커를 고릅니다.

```bash
python -m central_worker.central_worker
```

배포 매니페스트(StatefulSet·HPA·Service)는 비공개 저장소 `coldguard-deploy`의 `central-worker/`에 있습니다.

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
