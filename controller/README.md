# 제어 명령 검사기

AWS에서 받은 냉동고 설정 명령을 검사하고, 허용된 명령만 시뮬레이터에 전달하는 프로그램입니다.

명령을 전송한 것과 냉동고가 실제로 적용한 것을 구분하며, 적용 응답 또는 실패 사유를 기록합니다.

<br />

## 구조

```text
controller_svc.py    명령 검사 / 전달 / 적용 결과 처리 / 지표 제공
__init__.py          Python 패키지 구성
```

공통 설정 / 메시지 검증 / MQTT 및 DB 연결은 `common/`을 사용합니다.

<br />

## 주요 기능

- 시설 및 장비 확인
- 중복 명령 확인
- 명령 만료 및 최대 유효시간 검사
- 장비별 현장 잠금 상태 확인
- 설정 온도 허용 범위 검사
- 승인 명령 발행 및 실제 적용 응답 확인
- 명령 결정 및 차단 이벤트 기록
- 미전송 결과 재전송
- Prometheus 지표와 JSON 로그 제공

<br />

## 명령 기준

- 허용 동작 : `set_temp`
- 설정 온도 범위 : `-50°C 이상 / -10°C 미만`
- 설정 온도 : 소수 둘째 자리로 반올림하여 적용
- 최대 유효시간 : `issued_at`부터 `expires_at`까지 300초
- 만료된 명령은 실행하지 않음
- 미래 발급 시각에 대한 별도의 최대 5초 제한은 사용하지 않음
- 승인 명령에 원래 `expires_at`을 포함하여 전달
- 적용 응답 대기시간은 5초이며, 응답을 확인하지 못하면 `apply_timeout` 처리

현장 잠금 중에는 원격 온도 변경 명령을 거부합니다. 현장 웹의 문 열기 / 닫기는 잠금과 별개로 처리합니다.

<br />

## 실행

설치와 공통 설정 방법은 [최상위 README](../README.md)를 참고합니다.

프로젝트 최상위에서 실행합니다.

```powershell
python -m controller.controller_svc
```

<br />

## 환경변수

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `SITE` | 필수 | 담당 시설 ID |
| `DEVICES` | 필수 | 제어 가능한 장비 목록 / 예: `fz01,fz02` |
| `MQTT_HOST` | 필수 | MQTT 브로커 주소 |
| `MQTT_PORT` | 필수 | MQTT TLS 포트 / 기본값 `8883` |
| `MQTT_CA` | 필수 | CA 인증서 경로 |
| `CONTROLLER_USER` | 필수 | 제어기 MQTT 계정 / 기본값 `controller` |
| `CONTROLLER_PASSWORD` | 필수 | 제어기 MQTT 비밀번호 |
| `MQTT_USER` / `MQTT_PASS` | 선택 | 단독 실행에서 제어기 MQTT 계정 / 비밀번호를 덮어쓰는 값 |
| `MQTT_CLIENT_ID` | 선택 | 단독 실행용 클라이언트 ID / 기본값 `controller-<SITE>` |
| `MYSQL_HOST` | 필수 | MySQL 서버 주소 |
| `MYSQL_PORT` | 필수 | MySQL 포트 / 기본값 `3306` |
| `MYSQL_DB` | 필수 | DB 이름 / 기본값 `coldguard` |
| `CONTROLLER_DB_USER` | 필수 | MySQL 계정 / 기본값 `controller` |
| `CONTROLLER_DB_PASSWORD` | 필수 | MySQL 비밀번호 |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 선택 | 단독 실행에서 제어기 DB 계정 / 비밀번호를 덮어쓰는 값 |
| `METRICS_HOST` | 필수 | 지표 수신 주소 / 기본값 `0.0.0.0` |
| `CONTROLLER_METRICS_PORT` | 필수 | 지표 포트 / 기본값 `9202` |
| `METRICS_PORT` | 선택 | 단독 실행에서 지표 포트를 덮어쓰는 값 |
| `STATE_DIR` | 필수 | 명령 처리 및 재전송 상태 저장 폴더 / 기본값 `state/` |
| `STATE_DB` | 선택 | 단독 실행용 상태 DB 전체 경로 / 자동 생성 경로보다 우선 |

단독 실행에서는 공통 계정 및 포트 환경변수도 사용할 수 있습니다.

공통 계정 환경변수를 사용하면 대응하는 서비스별 계정 설정을 대체할 수 있습니다. MySQL 계정에는 SELECT / INSERT 권한이 필요합니다.

최대 명령 유효시간 300초는 현재 코드의 정책값이며, 환경변수 설정 항목은 아닙니다.

환경변수 예시는 [controller.env.example](../config/controller.env.example)을 참고합니다.

<br />

## MQTT 통신

| 종류 | 역할 |
| --- | --- |
| `cmd` | AWS의 원격 설정 명령 구독 |
| `apply` | 검증을 통과한 명령 발행 |
| `applied` | 냉동고의 실제 적용 응답 구독 |
| `ack` | 명령 처리 결과 발행 |
| `lock` | 현장 웹의 장비별 잠금 상태 구독 |
| `event` | 명령 차단 / 잠금 변경 이벤트 발행 |

QoS 1을 사용합니다. 현장 웹이 발행하는 `lock`만 retain true이며, 나머지는 retain false입니다.

<br />

## DB 및 지표

- MySQL 권한 : `SELECT / INSERT`
- 명령 결과 저장 : `command_log`
- 차단 및 잠금 이벤트 저장 : `event_log`
- 변경 전 설정 온도 확인에 측정 기록 사용

기본 지표 주소는 다음과 같습니다.

```text
http://실행서버주소:9202/metrics
```

<br />

## 연동 확인

- `cmd -> apply -> applied -> ack` 처리 확인
- 중복 / 만료 / 범위 초과 명령 차단 확인
- 현장 잠금 중 원격 명령 차단 확인
- 적용 응답이 없을 때 시간 초과 처리 확인
- DB 기록 및 MQTT 결과 발행 확인
- 재시작 후 기존 처리 정보 유지 확인
