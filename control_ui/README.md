# 통제 UI (KT 허브)

관리자가 로그인해 냉동고 설정 온도 명령을 보내고 결과를 확인하는 화면이다. 관제(보기)는 Grafana가, 통제(명령)는 이 화면이 맡는다.

## 구성
브라우저 → tailscale serve(https, 테일넷 안에서만) → 통제 UI(허브 `127.0.0.1:8081`) → 통제 API(테일넷 https, kt-ui 키)

- kt-ui 키는 허브의 파일에만 두고 브라우저로 보내지 않는다. 화면은 로그인 쿠키와 CSRF 토큰으로 이 서버만 부른다.
- 명령에는 로그인한 계정이 operator로 붙어 명령 기록에 `kt-ui:<계정ID>`로 남는다. 화면에서 operator를 바꿀 수 없다.
- 키·권한·형식 검사는 통제 API가, 판정(미등록 장비 → 중복 → 만료 → 잠금 → 허용 온도)은 시설 controller가 한다.
- 명령을 자동으로 다시 보내지 않는다. 실패하면 알리고, 다시 보낼지는 사람이 정한다.

## 화면
- 연결 상태: 통제 API·중앙 DB·중앙 MQTT. 하나라도 안 되거나 키 설정이 틀리면 명령 버튼을 막고 "원격 제어 중단"을 띄운다.
- 냉동고 상태(15초마다): 상태, 현재·설정 온도, 문(열림/닫힘), 냉각(가동/대기), 마지막 측정, 장비별 Grafana 링크(새 탭)
  - 상태는 현장 웹과 같은 기준이다. 측정이 `STALE_SEC`보다 오래되면 확인 불가(미래 시각은 collector처럼 `STALE_SEC`까지 허용), 고온(-25℃ 초과)이면 비정상, 그 밖에는 정상.
- 설정 온도 변경: 장비와 값 입력 → 확인창("fz01 설정 온도 -25.0 → -22.0℃로 바꿉니다") → 보내기 → 결과를 2초마다 최대 10분 확인(시설 단절 뒤 늦게 오는 결과도 받는다)
- 명령 이력(최근 50건): 요청 시각, 장비, 값, 명령자, 결과·사유, 판정 시각
- `?device=fz03`을 붙여 열면 그 장비를 미리 고른다.

## 보안
- 로그인 필수, 자가 가입 없음. 비밀번호 해시는 계정 주인이 계정 도구로 만들고, 계정 파일은 관리자가 배포한다.
- 비밀번호: 12자 이상, 영문 대문자·소문자·숫자·기호 포함, 계정 ID 미포함. 해시(scrypt)만 저장한다.
- 같은 계정 ID로 5번 연속 실패하면 5분 잠금. 없는 계정 ID도 똑같이 잠가서 계정이 있는지 드러나지 않는다.
- 세션: 2시간 동안 사용하지 않거나 로그인 후 하루가 지나면 끝난다. 15초 자동 갱신은 사용 시간에 넣지 않는다(화면을 켜 둔 채 자리를 비우면 로그아웃).
- 쿠키 `__Host-cg-ui`: Secure·HttpOnly·SameSite=Strict. POST는 CSRF 토큰(`X-CSRF-Token`)과 `Sec-Fetch-Site: same-origin`을 확인한다.
- 명령: 사용자당 1분에 10건.
- 응답 헤더: CSP(이 서버의 스크립트만), 프레임 금지, Referrer 없음. HSTS는 보내지 않는다(같은 호스트의 Grafana가 http라서 막히지 않게).
- 계정 파일을 바꾸면 재시작 없이 반영되고, 지우거나 비밀번호를 바꾼 계정의 세션은 바로 끝난다.
- 세션·잠금·명령 제한은 메모리에 둔다. gunicorn 작업 프로세스는 1개여야 하고, 다시 시작하면 모두 다시 로그인한다.

## 엔드포인트 (화면이 부르는 것)
| 메서드·경로 | 설명 |
|---|---|
| `GET /` | 화면 |
| `GET /healthz` | 프로세스 생존(인증 없음) |
| `GET /api/session` | 로그인 정보와 CSRF 토큰 |
| `POST /api/login` | `{"username", "password"}` |
| `POST /api/logout` | 로그아웃 |
| `GET /api/status` | `api`, `db`, `mqtt`, `key`, `can_command` |
| `GET /api/devices` | 장비 상태와 표기(`state`, `state_label`, `state_detail`, `grafana_url`) |
| `GET /api/commands?device_id=` | 명령 이력(최근 50건) |
| `GET /api/commands/{cmd_id}` | 명령 하나와 결과 |
| `POST /api/commands` | `{"site_id", "device_id", "value"}` (operator는 서버가 붙인다) |

오류(`{"error": 코드}`):
- 401 `unauthorized`·`session_expired`: 다시 로그인
- 403 `csrf_failed`·`cross_site`
- 429 `locked`·`rate_limited`: `retry_after`(초)와 `Retry-After` 헤더
- 502 `upstream_auth`: 통제 API가 kt-ui 키를 거부(키 설정 문제)
- 502 `api_unreachable`: 통제 API에 연결하지 못함. 명령이면 `sent: false`(나가지 않음)
- 504 `api_no_response`: 명령을 보냈지만 응답이 없음(처리됐는지 모름, 명령 이력으로 확인)
- 400·404·503: 통제 API의 오류 코드를 그대로 넘긴다(`mqtt_unavailable`, `db_unavailable`, `publish_unconfirmed`+`cmd_id` 등).

## 설정
환경변수는 [`config/control_ui.env.example`](../config/control_ui.env.example) 참고. `CONTROL_API_URL`과 `STALE_SEC`은 필수다.

키 파일과 계정 파일은 권한 600이어야 기동한다(그룹·다른 사용자가 읽을 수 있으면 거부). 키 파일에는 키 한 줄만 두며, 끝의 줄바꿈(`\r` 포함)은 서버가 지운다.

## 허브 배포
허브에는 모니터링 스택과 따로 compose 파일 하나로 올린다. 이미지 받기, 비밀 파일 배포, 실행(`docker compose up`), tailscale serve는 IaC 담당의 Ansible이 한 곳에서 맡는다. 이미지를 받는 곳과 실행하는 곳이 같아야 digest를 한 군데서만 바꾸면 되기 때문이다.

| 허브 경로 | 내용 | 권한 |
|---|---|---|
| `/opt/coldguard-control-ui/compose.yaml` | 아래 compose 파일 | |
| `/opt/coldguard-control-ui/secrets/` | 비밀 파일 폴더(컨테이너의 `/etc/control-ui`에 읽기 전용으로 마운트) | 700, uid·gid 10001 |
| `/opt/coldguard-control-ui/secrets/api-key` | kt-ui 키 한 줄 | 600, uid·gid 10001 |
| `/opt/coldguard-control-ui/secrets/accounts.json` | 계정 파일 | 600, uid·gid 10001 |

- 폴더도 컨테이너 사용자(uid 10001)의 것이어야 한다. 파일만 600이고 폴더가 root 전용이면 컨테이너가 파일을 읽지 못한다.
- 비밀 파일 두 개는 Ansible Vault에서 배포한다. 허브에서 직접 고치면 다음 Ansible 실행 때 덮어쓰인다.
- 이미지는 허브 전용 Pull 사용자(`coldguard-prod-kt-hub-ecr-pull`)로 ECR에 로그인한 뒤 digest로 받는다. ECR 로그인 토큰은 12시간만 유효하다.

### 계정 만들기·비밀번호 바꾸기
비밀번호는 계정 주인이 자기 PC에서 직접 넣고, 해시만 든 파일을 IaC 담당에게 보낸다(디스코드가 아닌 별도 경로). IaC 담당은 받은 파일들을 JSON 객체 하나로 합쳐 Vault에 넣는다.

```bash
python -m pip install werkzeug
python -m control_ui.accounts --file ../monitoring.json add monitoring
```

- 앱 저장소 맨 위 폴더에서 실행한다. werkzeug 말고 다른 패키지는 필요 없다.
- 파일은 저장소 밖(한 단계 위 폴더)에 만든다. 저장소 안에 두면 실수로 커밋될 수 있다.
- Windows에서는 PowerShell이나 cmd에서 실행한다. Git Bash에서는 입력하는 비밀번호가 화면에 보일 수 있다.
- 비밀번호를 바꿀 때는 같은 파일에 `passwd`(예: `--file ../monitoring.json passwd monitoring`)를 쓰고, 같은 방법으로 보낸다.
- 서버는 계정 파일이 바뀌면 재시작 없이 다시 읽고, 지웠거나 비밀번호가 바뀐 계정의 로그인 세션을 끝낸다.

### compose 파일
```yaml
name: coldguard-control-ui
services:
  control-ui:
    image: <ECR 레지스트리>/coldguard-prod-control-ui@sha256:<digest>
    # 작업 프로세스는 1개여야 한다(세션을 메모리에 둔다). tests/test_control_ui.py의 GUNICORN_ARGS와 같다.
    command: ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8081", "--workers", "1",
              "--worker-class", "gthread", "--threads", "8", "--worker-tmp-dir", "/tmp",
              "--graceful-timeout", "20", "--no-control-socket", "control_ui.app:create_app()"]
    environment:
      CONTROL_API_URL: https://control-api.<tailnet>.ts.net
      STALE_SEC: "<시설과 같은 값>"
      GRAFANA_DEVICE_URL: http://coldguard-kt-hub.<tailnet>.ts.net:3000/d/cg-dev?var-site={site}&var-device={device}
    volumes:
      - ./secrets:/etc/control-ui:ro
    ports:
      - "127.0.0.1:8081:8081"  # 화면은 허브 안에서만 연다. 테일넷에는 tailscale serve로 붙인다
      # 9205(/metrics)는 내보내지 않는다. 같은 docker 네트워크의 Prometheus가 control-ui:9205로 수집한다
    networks: [monitoring]
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8081/healthz"]
      interval: 30s
      timeout: 5s
    restart: unless-stopped

networks:
  monitoring:
    external: true  # 모니터링 compose가 만든 네트워크에 붙는다
    name: <모니터링 네트워크 이름>
```

- Prometheus 수집 대상은 `control-ui:9205`, 상태 점검은 `http://control-ui:8081/healthz`다. 같은 네트워크 안에서 서비스 이름으로 찾는다.
- 테일넷 공개는 허브에서 `tailscale serve --bg --https=8080 http://127.0.0.1:8081`로 한다. 주소는 `https://coldguard-kt-hub.<tailnet>.ts.net:8080`이고, 접속은 보안 ACL(control-users → kt-hub:8080)이 정한다. Funnel은 켜지 않는다.
- 컨테이너에서 통제 API 주소의 이름 풀이가 되는지 배포 때 확인한다. 화면의 연결 상태가 "끊김"이면 로그의 `control_ui_api_unreachable`을 본다.

## 지표 (`METRICS_PORT`, 기본 9205)
| 이름 | 라벨 `result` |
|---|---|
| `coldguard_ui_logins_total` | `success`, `fail`(잠기게 된 실패 포함), `locked`(잠긴 동안의 시도) |
| `coldguard_ui_commands_total` | `sent`, `publish_unconfirmed`, `mqtt_unavailable`, `db_unavailable`, `invalid_request`, `rate_limited`, `upstream_auth`, `api_unreachable`, `api_no_response`, `api_error` |

모든 결과가 처음부터 0으로 보인다. 로그인 전·CSRF에서 막힌 요청은 명령 수에 넣지 않고 로그(`ui_request_rejected`)로 남긴다.

## 로그 (한 줄 JSON, stdout)
- `ui_login`: user, result(success·fail·locked), remote. 형식이 틀린 ID는 user를 남기지 않는다(비밀번호를 ID 칸에 넣었을 수 있어서).
- `ui_logout`: user, reason(user·idle·max_age·account_changed)
- `ui_command`: user, remote, site_id, device_id, value, result, error, cmd_id
- `ui_request_rejected`: reason(csrf·cross_site), user, path, remote
- `control_ui_api_unreachable`·`control_ui_api_rejected`·`control_ui_api_bad_response`: 통제 API 호출 문제
- `control_ui_start`·`control_ui_config_error`·`control_ui_accounts_loaded`·`control_ui_accounts_error`

remote는 tailscale serve가 붙인 접속 기기의 테일넷 IP(`X-Forwarded-For`)다. 비밀번호·키·세션 ID·CSRF 토큰은 남기지 않는다.

## 시험
```bash
python -m unittest tests.test_control_ui -v
```
실제 통제 API에 연결하지 않는다. gunicorn 기동 시험은 gunicorn·prometheus_client가 설치된 Linux에서만 돈다.
