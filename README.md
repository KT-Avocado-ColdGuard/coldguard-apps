# coldguard-apps

ColdGuard의 냉동고 시뮬레이터 / 데이터 수집기 / 제어 명령 검사기 / Flask 현장 웹을 관리하는 저장소입니다.

시뮬레이터가 MQTT로 측정값을 전송하면 수집기가 MySQL에 저장합니다. 제어기는 AWS의 설정 명령을 검사해 냉동고로 전달하며, 현장 웹은 상태 조회와 문 조작 / 현장 잠금을 담당합니다.

<br />

## 구조

```text
common/             공통 설정 / 메시지 검증 / 연결 관리 / 온도 분석
simulator/          냉동고 시뮬레이터
collector/          데이터 수집기
controller/         제어 명령 검사기
field_web/          Flask 현장 웹 및 HTML
config/             서비스별 환경변수 예시
image/              냉동고 이미지
main.py             로컬 연동 확인용 통합 실행
requirements.txt
```

<br>

## 프로그램별 안내

- [냉동고 시뮬레이터](simulator/README.md)
- [데이터 수집기](collector/README.md)
- [제어 명령 검사기](controller/README.md)
- [Flask 현장 웹](field_web/README.md)

DB 스키마와 배포 자료는 별도 저장소 `coldguard-gitops`에서 관리합니다.

<br />

## 설치

프로젝트 최상위에서 실행합니다. 명령은 Windows PowerShell 기준입니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

.\.venv\Scripts\Activate.ps1
```

<br />

## 공통 설정 방식

- 환경변수가 있으면 우선 적용하고, 없으면 `common/coldguard_config.py`의 기본값을 사용합니다.
- 같은 항목에 공통 환경변수와 서비스별 환경변수가 모두 있으면 공통 환경변수가 우선합니다.
- `.env` 및 `config/*.env.example` 파일은 자동으로 읽지 않습니다.
- 설정을 변경하면 프로그램을 재시작해야 합니다.
- 빈 환경변수도 기본값을 덮어씁니다. 기본값을 사용하려면 해당 환경변수를 제거합니다.
- 불리언 환경변수는 `true/false`, `1/0`, `yes/no`, `on/off`를 사용할 수 있습니다.
- 시설 ID는 `site01`, 장비 ID는 `fz01`처럼 전체 값을 지정합니다.
- 실제 비밀번호는 Git이나 이미지에 포함하지 않고 실행 환경에서 주입합니다.

<br />

## 통합 실행

한 PC에서 모든 프로그램의 연동을 확인할 때 사용합니다.

```powershell
python main.py
```

- 네 서비스의 설정 및 인증서 경로 등을 먼저 검사합니다.
- 사전 검사에 실패하면 서비스를 시작하지 않습니다.
- `SIM_MQTT=True`와 네 서비스 모두의 설정이 필요합니다.
- 실제 MQTT / MySQL 연결 성공은 각 프로그램의 로그로 확인합니다.
- `Ctrl+C`를 누르거나 서비스 하나가 종료되면 나머지도 종료합니다.
- 이미 같은 시설의 수집기와 제어기가 실행 중이면 중복 실행하지 않습니다.

통합 실행에서는 서비스별 계정과 포트 설정을 사용해야 합니다. 다음 공통 환경변수는 충돌을 방지하기 위해 통합 실행에서 거부합니다.

```text
MQTT_USER
MQTT_PASS
MYSQL_USER
MYSQL_PASSWORD
MQTT_CLIENT_ID
WEB_PORT
METRICS_PORT
STATE_DB
```

<br />

## 환경변수

`python main.py`는 시뮬레이터 / 수집기 / 제어기 / 현장 웹을 함께 실행합니다.

환경변수가 있으면 우선 사용하고, 없으면 `common/coldguard_config.py`의 기본값을 사용합니다. `.env` 파일은 자동으로 읽지 않습니다.

아래의 **필수**는 환경변수 또는 설정 파일에 유효한 값이 있어야 한다는 뜻입니다. 기본값이 있는 항목은 그대로 사용해도 됩니다.

비밀번호와 보안 키는 코드나 이미지에 포함하지 않고 실행할 때 주입합니다.

<br />

### 시설 및 연결 설정

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `SITE` | 필수 | 시설 ID / 기본값 `site01` |
| `DEVICE` | 필수 | 시뮬레이터가 담당하는 냉동고 ID / 기본값 `fz01` |
| `DEVICES` | 필수 | 시설 서비스의 관리 장비 목록 / 예: `fz01,fz02` / 반드시 `DEVICE` 포함 |
| `SIM_MQTT` | 필수 | 통합 실행에서는 `true` 지정 / 기본값은 `false` |
| `MQTT_HOST` | 필수 | 네 서비스가 연결할 MQTT 브로커 주소 |
| `MQTT_PORT` | 필수 | MQTT TLS 포트 / 기본값 `8883` |
| `MQTT_CA` | 필수 | 실행 환경에서 읽을 수 있는 CA 인증서 파일 경로 |
| `MYSQL_HOST` | 필수 | MySQL 서버 주소 |
| `MYSQL_PORT` | 필수 | MySQL 포트 / 기본값 `3306` |
| `MYSQL_DB` | 필수 | DB 이름 / 기본값 `coldguard` |
| `STALE_SEC` | 필수 | 수집기와 현장 웹의 데이터 수신 지연 판단 기준 / 초 |

<br />

### 서비스별 계정

통합 실행에서는 각 프로그램이 서로 다른 계정을 사용할 수 있도록 서비스별 변수명을 사용합니다.

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `SIM_PASSWORD` | 필수 | 시뮬레이터 장비 계정의 MQTT 비밀번호 / 계정 이름은 `DEVICE` |
| `COLLECTOR_USER` | 필수 | 수집기 MQTT 계정 / 기본값 `collector` |
| `COLLECTOR_PASSWORD` | 필수 | 수집기 MQTT 비밀번호 |
| `CONTROLLER_USER` | 필수 | 제어기 MQTT 계정 / 기본값 `controller` |
| `CONTROLLER_PASSWORD` | 필수 | 제어기 MQTT 비밀번호 |
| `WEB_USER` | 필수 | 현장 웹 MQTT 계정 / 기본값 `web` |
| `WEB_PASSWORD` | 필수 | 현장 웹 MQTT 비밀번호 |
| `COLLECTOR_DB_USER` | 필수 | 수집기 MySQL 계정 / 기본값 `collector` |
| `COLLECTOR_DB_PASSWORD` | 필수 | 수집기 MySQL 비밀번호 |
| `CONTROLLER_DB_USER` | 필수 | 제어기 MySQL 계정 / 기본값 `controller` |
| `CONTROLLER_DB_PASSWORD` | 필수 | 제어기 MySQL 비밀번호 |
| `WEB_DB_USER` | 필수 | 현장 웹 MySQL 계정 / 기본값 `web` |
| `WEB_DB_PASSWORD` | 필수 | 현장 웹 MySQL 비밀번호 | 

<br />

### 웹 / 지표 / 상태 저장

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `WEB_HOST` | 필수 | 시뮬레이터와 현장 웹의 수신 주소 / 기본값 `0.0.0.0` |
| `SIM_WEB_PORT` | 필수 | 시뮬레이터 웹 포트 / 기본값 `5000` |
| `FIELD_WEB_PORT` | 필수 | 현장 웹 포트 / 기본값 `8080` |
| `WEB_SECRET` | 필수 | 현장 웹 보안 키 / 32자 이상 |
| `WEB_ALLOW_NETS` | 필수 | 현장 웹 접속을 허용할 IP 또는 CIDR 대역 |
| `WEB_MQTT` | 선택 | 현장 웹 MQTT 사용 여부 / 전체 연동 확인 시 `true` / 기본값 `true` |
| `WEB_CONTROL` | 선택 | 현장 웹 제어 기능 사용 여부 / 제어 확인 시 `true` / 기본값 `true` |
| `METRICS_HOST` | 필수 | 수집기와 제어기의 지표 수신 주소 / 기본값 `0.0.0.0` |
| `COLLECTOR_METRICS_PORT` | 필수 | 수집기 지표 포트 / 기본값 `9201` |
| `CONTROLLER_METRICS_PORT` | 필수 | 제어기 지표 포트 / 기본값 `9202` |
| `STATE_DIR` | 필수 | 상태 저장 폴더 / 기본값은 프로젝트 최상위 `state/` |
| `SIM_DB` | 선택 | 시뮬레이터 상태 DB 전체 경로 / 미지정 시 자동 생성 |

네 개의 웹 / 지표 포트는 서로 달라야 하며, 다른 프로그램이 사용 중인 포트와도 겹치지 않아야 합니다.

`STATE_DIR` 안에는 서비스 및 장비별 상태 파일이 생성됩니다. 재시작할 때 기존 파일을 유지해야 순번과 재전송 상태를 이어서 사용할 수 있습니다.

<br />

### 통합 실행에서 사용하지 않는 환경변수

다음 환경변수는 네 프로그램에 같은 값이 전달되는 문제를 방지하기 위해 `main.py`에서 거부합니다. 빈 값으로 지정한 경우도 포함합니다.

```text
MQTT_USER
MQTT_PASS
MYSQL_USER
MYSQL_PASSWORD
MQTT_CLIENT_ID
WEB_PORT
METRICS_PORT
STATE_DB
```

계정과 포트는 위 표의 서비스별 변수명을 사용합니다. MQTT 클라이언트 ID와 서비스 상태 파일 경로는 기본 생성 규칙을 사용합니다.

각 프로그램을 단독 실행할 때는 위 공통 환경변수도 사용할 수 있습니다.

## GitHub Actions CI

PR / main 변경 시 단위 테스트 -> 공통 이미지 빌드 -> Trivy 전체 보고서 -> HIGH / CRITICAL 판정을 실행합니다.
실행 / 보고서 다운로드 / 예외 / 실패 처리 방법은 [CI 안내](ci/README.md)를 참고하세요.
ECR 게시와 운영 배포는 포함하지 않습니다.
