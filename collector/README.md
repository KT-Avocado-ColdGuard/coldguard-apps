# 냉동고 데이터 수집기

MQTT로 냉동고 측정값을 받아 MySQL에 저장하고, 온도 변화와 데이터 수신 상태를 분석하는 프로그램입니다.

시설의 여러 냉동고를 `DEVICES` 목록으로 관리합니다.

<br />

## 구조

```text
data_collector.py    측정값 수신 / 저장 / 경보 판단 / 지표 제공
__init__.py          Python 패키지 구성
```

공통 설정과 연결 관리는 `common/`을 사용합니다. 온도 예측 계산은 `common/temperature_analysis.py`를 사용합니다.

<br />

## 주요 기능

- 측정 메시지의 필드 / 시설 / 장비 / 온도 / 시각 검사
- MySQL 측정값 저장
- 시설 ID / 장비 ID / 순번을 기준으로 중복 저장 방지
- 늦게 도착한 측정값이 최신 상태를 덮어쓰지 않도록 처리
- 온도 상승 추세와 위험 온도 도달 예상 시간 계산
- 조기 경보 / 고온 / 데이터 수신 중단 이벤트 처리
- 경보 이벤트 저장 및 MQTT 발행
- Prometheus 지표와 JSON 로그 제공

고온 기준은 `-25°C 초과`입니다. `-25.0°C`는 고온 상태로 판단하지 않습니다.

<br />

## 실행

설치와 공통 설정 방법은 [최상위 README](../README.md)를 참고합니다.

프로젝트 최상위에서 실행합니다.

```powershell
python -m collector.data_collector
```

<br />

## 환경변수

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `SITE` | 필수 | 담당 시설 ID |
| `DEVICES` | 필수 | 관리 장비 목록 / 쉼표로 구분 / 예: `fz01,fz02` |
| `MQTT_HOST` | 필수 | MQTT 브로커 주소 |
| `MQTT_PORT` | 필수 | MQTT TLS 포트 / 기본값 `8883` |
| `MQTT_CA` | 필수 | CA 인증서 경로 |
| `COLLECTOR_USER` | 필수 | 수집기 MQTT 계정 / 기본값 `collector` |
| `COLLECTOR_PASSWORD` | 필수 | 수집기 MQTT 비밀번호 |
| `MQTT_USER` / `MQTT_PASS` | 선택 | 단독 실행에서 수집기 MQTT 계정 / 비밀번호를 덮어쓰는 값 |
| `MQTT_CLIENT_ID` | 선택 | 단독 실행용 클라이언트 ID / 기본값 `collector-<SITE>` |
| `MYSQL_HOST` | 필수 | MySQL 서버 주소 |
| `MYSQL_PORT` | 필수 | MySQL 포트 / 기본값 `3306` |
| `MYSQL_DB` | 필수 | DB 이름 / 기본값 `coldguard` |
| `COLLECTOR_DB_USER` | 필수 | MySQL 계정 / 기본값 `collector` |
| `COLLECTOR_DB_PASSWORD` | 필수 | MySQL 비밀번호 |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 선택 | 단독 실행에서 수집기 DB 계정 / 비밀번호를 덮어쓰는 값 |
| `STALE_SEC` | 필수 | 측정값 수신 중단 판단 기준 / 초 / 배포 전 지정 |
| `METRICS_HOST` | 필수 | 지표 수신 주소 / 기본값 `0.0.0.0` |
| `COLLECTOR_METRICS_PORT` | 필수 | 지표 포트 / 기본값 `9201` |
| `METRICS_PORT` | 선택 | 단독 실행에서 지표 포트를 덮어쓰는 값 |
| `STATE_DIR` | 필수 | 상태 저장 폴더 / 기본값 `state/` |
| `STATE_DB` | 선택 | 단독 실행용 상태 DB 전체 경로 / 자동 생성 경로보다 우선 |

단독 실행에서는 공통 계정 및 포트 환경변수도 사용할 수 있습니다.

공통 계정 환경변수를 사용하면 대응하는 서비스별 계정 설정을 대체할 수 있습니다. MySQL 계정에는 SELECT / INSERT 권한이 필요합니다.

환경변수 예시는 [collector.env.example](../config/collector.env.example)을 참고합니다.

<br />

## MQTT 및 DB

- 구독 : 관리 장비의 `telemetry`
- 발행 : 관리 장비의 `event`
- MQTT : QoS 1 / retain false
- MySQL 권한 : `SELECT / INSERT`
- 사용하는 테이블 : `telemetry` / `event_log`

관리자가 DB 스키마를 미리 생성해야 합니다. 프로그램이 테이블을 자동 생성하지 않습니다.

<br />

## 지표 및 로그

기본 지표 주소는 다음과 같습니다.

```text
http://실행서버주소:9201/metrics
```

온도 / 위험 온도까지 예상 시간 / 마지막 측정 시각 / 데이터 가용 상태 / 경보 상태 등의 지표를 제공합니다.

프로그램 로그는 수집하기 쉬운 한 줄 JSON 형식으로 출력합니다.

<br />

## 연동 확인

- 측정 토픽 구독 성공 확인
- MySQL에 장비별 측정값 저장 확인
- 같은 순번 재수신 시 중복 저장되지 않는지 확인
- 고온 / 조기 경보 / 데이터 수신 중단 이벤트 확인
- `/metrics` 접근 및 지표 갱신 확인
- 연결 복구 후 재처리 동작 확인
