# Flask 현장 웹

시설의 냉동고 상태와 기록을 브라우저에서 확인하는 프로그램입니다.

관리 장비를 선택하여 온도와 문 상태를 조회하고, 문 열기 / 닫기 및 현장 잠금을 요청할 수 있습니다.

<br />

## 구조

```text
field_web.py                Flask 서버 / 상태 조회 / 제어 요청
templates/field_web.html    웹 화면
__init__.py                 Python 패키지 구성
```

공통 설정과 연결 관리는 `common/`을 사용합니다. 온도 분석은 `common/temperature_analysis.py`, 냉동고 이미지는 최상위 `image/`를 사용합니다.

<br />

## 주요 기능

- 관리 냉동고 선택
- 현재 온도 / 설정 온도 / 문 상태 / 냉각 상태 조회
- 측정 기록 / 명령 결과 / 이벤트 조회
- 문 열기 / 닫기 요청
- 냉동고별 현장 잠금 설정 및 해제
- 접속 허용 네트워크 검사
- CSRF 검증을 통한 제어 요청 보호

현장 잠금은 AWS 원격 온도 변경 명령을 차단하는 기능입니다. 문 열기 / 닫기는 잠금 상태와 무관하게 허용합니다.

제어 요청을 전송한 사실과 실제 장비 상태 변경은 구분합니다. 실제 변경 여부는 이후 수신되는 상태와 결과를 확인해야 합니다.

<br />

## 실행

설치와 공통 설정 방법은 [최상위 README](../README.md)를 참고합니다.

프로젝트 최상위에서 실행합니다.

```powershell
python -m field_web.field_web
```

기본 웹 포트는 `8080`입니다.

```text
http://실행서버주소:8080
```

접속하는 클라이언트의 IP가 `WEB_ALLOW_NETS`에서 허용되어 있어야 합니다.

<br />

## 환경변수

| 이름 | 필수 | 설명 |
| --- | --- | --- |
| `SITE` | 필수 | 담당 시설 ID |
| `DEVICES` | 필수 | 조회 및 제어할 장비 목록 / 예: `fz01,fz02` |
| `WEB_HOST` | 필수 | 웹 수신 주소 / 기본값 `0.0.0.0` |
| `FIELD_WEB_PORT` | 필수 | 웹 포트 / 기본값 `8080` |
| `WEB_PORT` | 선택 | 단독 실행에서 웹 포트를 덮어쓰는 값 |
| `WEB_SECRET` | 필수 | 웹 보안 키 / 32자 이상 / Secret으로 주입 |
| `WEB_ALLOW_NETS` | 필수 | 접속 허용 IP 또는 CIDR 대역 |
| `WEB_MQTT` | 선택 | MQTT 사용 여부 / 기본값 `true` |
| `WEB_CONTROL` | 선택 | 제어 기능 사용 여부 / 기본값 `true` |
| `MQTT_HOST` | MQTT 사용 시 | 브로커 주소 |
| `MQTT_PORT` | MQTT 사용 시 | MQTT TLS 포트 / 기본값 `8883` |
| `MQTT_CA` | MQTT 사용 시 | CA 인증서 경로 |
| `WEB_USER` | MQTT 사용 시 | MQTT 계정 / 기본값 `web` |
| `WEB_PASSWORD` | MQTT 사용 시 | MQTT 비밀번호 |
| `MQTT_USER` / `MQTT_PASS` | 선택 | 단독 실행에서 웹 MQTT 계정 / 비밀번호를 덮어쓰는 값 |
| `MQTT_CLIENT_ID` | 선택 | 단독 실행용 클라이언트 ID / 기본값 `web-<SITE>` |
| `MYSQL_HOST` | 필수 | MySQL 서버 주소 |
| `MYSQL_PORT` | 필수 | MySQL 포트 / 기본값 `3306` |
| `MYSQL_DB` | 필수 | DB 이름 / 기본값 `coldguard` |
| `WEB_DB_USER` | 필수 | 조회 전용 MySQL 계정 / 기본값 `web` |
| `WEB_DB_PASSWORD` | 필수 | MySQL 비밀번호 |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 선택 | 단독 실행에서 웹 DB 계정 / 비밀번호를 덮어쓰는 값 |
| `STALE_SEC` | 필수 | 측정값이 오래되었다고 판단할 기준 시간 / 초 |

단독 실행에서는 공통 계정 및 웹 포트 환경변수도 사용할 수 있습니다.

공통 계정 환경변수를 사용하면 대응하는 서비스별 계정 설정을 대체할 수 있습니다.

WEB_HOST=0.0.0.0은 모든 인터페이스에서 연결을 받는다는 뜻입니다. 브라우저에서는 실행 서버의 실제 주소로 접속합니다.

문 조작과 잠금 요청에는 MQTT 연결 및 해당 토픽의 발행 권한이 필요합니다.

환경변수 예시는 [field_web.env.example](../config/field_web.env.example)을 참고합니다.

<br />

## MQTT 및 DB

- 문 조작 발행 : `coldguard/<시설 ID>/<장비 ID>/door`
- 현장 잠금 발행 : `coldguard/<시설 ID>/<장비 ID>/lock`
- 문 조작 : QoS 1 / retain false
- 현장 잠금 : QoS 1 / retain true
- MySQL 권한 : `SELECT`

현장 웹은 DB에 측정값이나 명령 결과를 직접 쓰지 않습니다.

문 조작과 잠금 기능을 사용하려면 MQTT 연결 및 토픽 권한이 필요합니다. 문 조작은 시뮬레이터가, 잠금에 따른 원격 명령 차단은 제어기가 처리합니다.

<br />

## 실행에 필요한 구성

- `field_web.py`
- `templates/field_web.html`
- 최상위 `common/`
- 최상위 `image/`
- `requirements.txt`에 정의된 의존성
- MQTT / MySQL 및 웹 설정

HTML은 Flask 템플릿이므로 파일을 브라우저에서 직접 여는 방식으로는 전체 기능을 확인할 수 없습니다.

<br />

## 연동 확인

- 허용된 네트워크에서 웹 접속 확인
- 장비 목록 및 최신 측정 상태 조회 확인
- 기록 조회 확인
- 문 조작 후 실제 시뮬레이터 상태 변경 확인
- 현장 잠금 설정 후 AWS 원격 명령 차단 확인
- 잠금 해제 후 원격 명령 처리 확인
- MQTT 또는 측정값 수신이 중단되었을 때 화면 상태 확인
