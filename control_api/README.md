# 통제 API (기반 세팅만 된 상태)

중앙 허브에서 운영자 명령을 받아 `cmd`로 발행하고 시설의 `ack`를 받는 서비스의 뼈대입니다.
API 형식·인증·배포는 아직 정하지 않았습니다.

## 지금 되는 것
- Flask 서버 (`python -m control_api.app`, 기본 127.0.0.1:8000)
- MQTT TLS 연결, 자동 재접속 (paho-mqtt 2.x)
- `coldguard/+/+/ack` 구독: 받은 ack를 표준 출력에 찍기만 함 (`app.py`의 `on_ack`)
- `GET /healthz`: MQTT 연결 시 200 `{"status":"ok","mqtt":true}`, 아니면 503

## 파일
- `config.py`: 환경변수로 설정 읽기 (필수값 없으면 기동 거부)
- `bus.py`: MQTT 연결·ack 구독 (`MqttBus`)
- `app.py`: Flask 앱, `/healthz`, `main()`

## 환경변수
| 이름 | 필수 | 예 |
|---|---|---|
| MQTT_HOST | O | 중앙 브로커 주소 (시험 때 시설 100.114.239.104) |
| MQTT_PORT | | 8883 |
| MQTT_CA | O | CA 파일 경로 (시설 CA와 같은 CA) |
| MQTT_USER / MQTT_PASS | O | 브로커 계정 (cmd 쓰기, ack 읽기 권한 필요) |
| MQTT_CLIENT_ID | | control-api |
| CONTROL_API_PORT | | 8000 |

## 실행 (Windows PowerShell 예)
~~~
python -m venv .venv
.\.venv\Scripts\python -m pip install Flask==3.1.3 paho-mqtt==2.1.0
$env:MQTT_HOST="..."; $env:MQTT_CA="...\ca.crt"; $env:MQTT_USER="..."; $env:MQTT_PASS="..."
.\.venv\Scripts\python -m control_api.app
Invoke-RestMethod http://localhost:8000/healthz
~~~
시설 브로커의 `aws-bridge` 계정(cmd 쓰기·ack 읽기)으로 연결 시험을 마쳤습니다 (11/25).

## 다음에 정할 것
1. 명령 API 형식: 주소, 요청 필드(site_id, device_id, value, issued_by 등), 응답·오류 코드
2. cmd 생성 규칙: 규격 v1.4 cmd 형식, cmd_id 규칙, 만료 시간 (`common/coldguard_protocol.py`의 `identity`, `topic`, `utc` 사용 권장)
3. 인증: 호출 측(KT 관제 등) 인증 방식
4. ack 결과 저장·조회: 메모리 / 중앙 MySQL
5. 중앙 브로커 계정, EKS 배포(Dockerfile, CI, Tailscale operator 노출), 감사 로그 형식
