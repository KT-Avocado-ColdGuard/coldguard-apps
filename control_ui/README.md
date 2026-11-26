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
- 로그인 필수, 자가 가입 없음. 계정은 관리자가 계정 도구로 만든다.
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
허브 compose 폴더(`/opt/coldguard-hub`)에 비밀 파일 폴더를 만들고 키 파일을 넣는다. 주인은 컨테이너 사용자(uid 10001)다.

```bash
install -d -m 700 -o 10001 -g 10001 /opt/coldguard-hub/control-ui
install -m 600 -o 10001 -g 10001 api-key /opt/coldguard-hub/control-ui/api-key
```

계정은 같은 이미지로 한 번 실행해 만든다. 비밀번호는 계정 주인이 직접 입력한다.

```bash
docker run --rm -it -v /opt/coldguard-hub/control-ui:/etc/control-ui <이미지> python -m control_ui.accounts add <계정ID>
```

`passwd`(비밀번호 변경), `remove`(삭제), `list`(목록)도 같은 방식이다. 서버가 떠 있어도 바로 반영된다.

compose 서비스 예시:

```yaml
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
      - ./control-ui:/etc/control-ui:ro
    ports:
      - "127.0.0.1:8081:8081"  # 화면은 허브 안에서만 연다. 테일넷에는 tailscale serve로 붙인다
      # 9205(/metrics)는 내보내지 않는다. 같은 docker 네트워크의 Prometheus가 control-ui:9205로 수집한다
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8081/healthz"]
      interval: 30s
      timeout: 5s
    restart: unless-stopped
```

- 모니터링 스택과 같은 docker 네트워크에 둔다(같은 compose 파일이면 기본 네트워크). Prometheus 수집 대상은 `control-ui:9205`, 상태 점검은 `http://control-ui:8081/healthz`다.
- 테일넷 공개는 허브에서 `tailscale serve --bg --https=8080 http://127.0.0.1:8081`로 한다. 주소는 `https://coldguard-kt-hub.<tailnet>.ts.net:8080`이고, 접속은 보안 ACL(control-users → kt-hub:8080)이 정한다. Funnel은 켜지 않는다.
- 컨테이너에서 통제 API 주소의 이름 풀이가 되는지 배포 때 확인한다(화면의 연결 상태가 "끊김"이면 로그의 `control_ui_api_unreachable`을 본다).

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
