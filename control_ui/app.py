"""중앙 통제 UI 서버 (Flask)

- 관리자가 로그인해 냉동고 설정 온도 명령을 보내고 결과를 확인하는 화면. KT 허브에서 연다.
- 브라우저 → 통제 UI 서버 → 통제 API. kt-ui 키는 이 서버의 파일에만 두고 브라우저로 보내지 않는다.
- 키·권한·형식 검사는 통제 API가, 허용 온도·만료·중복·잠금 판정은 시설 controller가 한다.
  이 서버는 로그인한 계정을 operator로 붙여 전달만 하고, 명령을 자동으로 다시 보내지 않는다.
- 장비 표기는 현장 웹과 같은 기준이다(상태 정상/비정상/확인 불가, 문 열림/닫힘, 냉각 가동/대기).

실행: gunicorn 작업 프로세스 1개(세션·잠금·명령 제한을 메모리에 둔다). control_ui/README.md 참고.
"""
import hmac
import math
import re
import time
from urllib.parse import quote, urlencode

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from common.coldguard_protocol import identity, loads, number
from common.coldguard_runtime import log
from control_ui import config as config_module
from control_ui.accounts import USER, AccountBook
from control_ui.guard import (CMD_LIMIT, CMD_WINDOW_SEC, IDLE_SEC, MAX_AGE_SEC, LoginGuard, RateLimit,
                              Sessions)
from control_ui.upstream import ControlApi, Unreachable, read_key

API = '/api/v1'
COOKIE = '__Host-cg-ui'  # __Host-: Secure·Path=/·Domain 없음이 강제되어 다른 호스트·http 페이지가 덮어쓸 수 없다
MAX_BODY = 4096
CMD_ID = re.compile(r'[A-Za-z0-9-]{1,50}')
COMMAND_FIELDS = ('site_id', 'device_id', 'value')
LOGIN_RESULTS = ('success', 'fail', 'locked')
COMMAND_RESULTS = ('sent', 'publish_unconfirmed', 'mqtt_unavailable', 'db_unavailable', 'invalid_request',
                   'rate_limited', 'upstream_auth', 'api_unreachable', 'api_no_response', 'api_error')
UPSTREAM_503 = ('mqtt_unavailable', 'db_unavailable', 'publish_unconfirmed')
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
NORMAL_TEMP = -27.0  # 현장 웹: 이 온도 이하로 정상이면 "정상적으로 운영 중입니다."


class UiError(Exception):
    def __init__(self, status, code, result=None, **extra):
        super().__init__(code)
        self.status, self.code, self.result, self.extra = status, code, result, extra


# ---------- 장비 표기 ----------

def device_view(d, stale_sec, grafana):
    """통제 API의 장비 한 줄에 화면 표기를 붙인다. 판정 기준은 현장 웹과 같다.
    - 측정이 stale_sec보다 오래됐으면 확인 불가(미래 시각은 collector처럼 stale_sec까지 허용)
    - 고온(status=high_temp, -25℃ 초과)이면 비정상, 아니면 정상"""
    age, temp, status = d.get('age_sec'), d.get('temp_c'), d.get('status')
    if (not number(age) or not number(temp) or not -stale_sec <= age <= stale_sec
            or status not in ('normal', 'high_temp')):
        state, label, detail = 'unknown', '확인 불가', '데이터 확인 불가'
    elif status == 'high_temp':
        state, label, detail = 'abnormal', '비정상', '고온 경보 - 위험 온도 도달'
    else:
        state, label = 'normal', '정상'
        detail = '정상적으로 운영 중입니다.' if temp <= NORMAL_TEMP else '정상 운전'
    view = {k: d.get(k) for k in ('site_id', 'device_id', 'temp_c', 'setpoint_c', 'door_open', 'cooling',
                                  'measured_at', 'age_sec', 'status')}
    view.update(state=state, state_label=label, state_detail=detail, grafana_url=None)
    try:
        site, device = identity(d.get('site_id'), d.get('device_id'))
    except ValueError:
        return view
    if grafana:
        view['grafana_url'] = grafana.replace('{site}', quote(site, safe='')).replace('{device}', quote(device, safe=''))
    return view


# ---------- 지표 ----------

class Metrics:
    """/metrics는 화면 포트와 다른 포트(기본 9205)로 연다. tailscale serve에는 화면 포트만 붙인다."""

    def __init__(self, cfg):
        from prometheus_client import CollectorRegistry, Counter, start_http_server
        r = CollectorRegistry()
        self._logins = Counter('coldguard_ui_logins_total', '로그인 시도 결과', ('result',), registry=r)
        self._commands = Counter('coldguard_ui_commands_total', '명령 요청 처리 결과', ('result',), registry=r)
        for result in LOGIN_RESULTS:  # 한 번도 없던 결과도 0으로 보이게(알림 규칙이 바로 걸리도록)
            self._logins.labels(result)
        for result in COMMAND_RESULTS:
            self._commands.labels(result)
        try:
            self.server, self.thread = start_http_server(cfg['METRICS_PORT'], addr=cfg['METRICS_HOST'],
                                                         registry=r)
        except OSError as exc:
            # 지표 포트를 못 열어도 화면과 명령은 계속한다(지표 수집만 빠진다).
            self.server = self.thread = None
            log('control_ui_metrics_unavailable', error=type(exc).__name__, port=cfg['METRICS_PORT'])

    def login(self, result):
        self._logins.labels(result).inc()

    def command(self, result):
        self._commands.labels(result).inc()


# ---------- 앱 ----------

def create_app(cfg=None, api=None, accounts=None, metrics=None, clock=None):
    """배포에서는 인자 없이 부른다(gunicorn "control_ui.app:create_app()"). 시험에서는 가짜 객체를 넣는다."""
    try:
        cfg = cfg or config_module.load()
        accounts = accounts or AccountBook(cfg['ACCOUNTS_FILE'])
        api = api or ControlApi(cfg['CONTROL_API_URL'], read_key(cfg['API_KEY_FILE']))
    except (ValueError, OSError) as exc:
        # 설정·키·계정 파일 오류는 기동을 멈춘다. 메시지에 키나 비밀번호 해시는 들어가지 않는다.
        log('control_ui_config_error', reason=str(exc))
        raise
    metrics = metrics or Metrics(cfg)
    clock = clock or time.monotonic
    sessions = Sessions(clock)
    guard = LoginGuard(accounts.has, clock)
    limit = RateLimit(clock=clock)
    log('control_ui_start', api=cfg['CONTROL_API_URL'], users=accounts.users(), stale_sec=cfg['STALE_SEC'],
        grafana=bool(cfg['GRAFANA_DEVICE_URL']), metrics_port=cfg['METRICS_PORT'])

    app = Flask(__name__, static_folder='static', static_url_path='/static')
    # tailscale serve가 붙이는 X-Forwarded-For(접속한 기기의 테일넷 IP)를 로그의 remote로 쓴다.
    # 화면 포트는 허브의 127.0.0.1에만 열려 serve를 거쳐서만 들어온다. remote는 기록용이고 권한 판단에 쓰지 않는다.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=0, x_host=0, x_port=0, x_prefix=0)
    app.config['MAX_CONTENT_LENGTH'] = MAX_BODY
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    # ----- 요청 검사 -----

    def body_json():
        if not request.is_json:
            raise UiError(415, 'json_required')
        data = request.get_data()
        # 길이를 알리지 않는 chunked 본문은 Werkzeug가 상한에서 잘라 넘기므로, 상한에 닿았으면 거부한다(통제 API와 같은 규칙).
        if len(data) >= MAX_BODY:
            raise UiError(413, 'request_entity_too_large')
        try:
            return loads(data)  # 중복 키·NaN·객체가 아닌 JSON은 거부(공통 규칙)
        except (ValueError, UnicodeError, RecursionError):
            raise UiError(400, 'invalid_json') from None

    def same_site(user=None):
        # 브라우저가 붙이는 Sec-Fetch-Site로 다른 사이트에서 보낸 요청을 거부한다.
        # 이 헤더가 없는 클라이언트도 SameSite=Strict 쿠키와 CSRF 토큰 검사로 막힌다.
        site = request.headers.get('Sec-Fetch-Site')
        if site is not None and site != 'same-origin':
            log('ui_request_rejected', reason='cross_site', user=user, path=request.path,
                remote=request.remote_addr)
            raise UiError(403, 'cross_site')

    def refresh_accounts():
        ended = accounts.refresh()
        if ended:
            sessions.drop_users(ended)
            for user in sorted(ended):
                log('ui_logout', user=user, reason='account_changed')

    def current(write):
        """로그인 세션. write(POST)는 같은 사이트 요청과 CSRF 토큰까지 확인한다.
        자동 갱신(GET)은 사용 시간에 넣지 않고, 화면이 X-User-Active: 1을 붙인 요청만 넣는다."""
        refresh_accounts()
        active = write or request.headers.get('X-User-Active') == '1'
        session, reason, expired_user = sessions.get(request.cookies.get(COOKIE), active)
        if session is None:
            if expired_user:
                log('ui_logout', user=expired_user, reason=reason)
            raise UiError(401, 'unauthorized' if reason == 'missing' else 'session_expired')
        if write:
            same_site(session.user)
            token = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(token.encode(), session.csrf.encode()):
                log('ui_request_rejected', reason='csrf', user=session.user, path=request.path,
                    remote=request.remote_addr)
                raise UiError(403, 'csrf_failed')
        return session

    def session_view(session):
        return dict(user=session.user, csrf=session.csrf, stale_sec=cfg['STALE_SEC'], idle_sec=IDLE_SEC,
                    max_age_sec=MAX_AGE_SEC, cmd_limit=CMD_LIMIT, cmd_window_sec=CMD_WINDOW_SEC)

    # ----- 통제 API 호출 -----

    def upstream(method, path, body=None):
        """(상태, 본문). 통제 API의 401·403은 화면 로그인 문제가 아니라 kt-ui 키 설정 문제라 502(upstream_auth)로 바꾼다."""
        where = path.split('?', 1)[0]
        try:
            status, data = api.call(method, path, body)
        except Unreachable as exc:
            log('control_ui_api_unreachable', method=method, path=where, error=exc.error, sent=exc.sent)
            if method == 'POST' and exc.sent:
                raise UiError(504, 'api_no_response', 'api_no_response') from None
            raise UiError(502, 'api_unreachable', 'api_unreachable',
                          **({'sent': False} if method == 'POST' else {})) from None
        if status in (401, 403):
            log('control_ui_api_rejected', method=method, path=where, status=status)
            raise UiError(502, 'upstream_auth', 'upstream_auth')
        if not isinstance(data, dict):
            log('control_ui_api_bad_response', method=method, path=where, status=status)
            raise UiError(502, 'api_error', 'api_error')
        return status, data

    def passed(status, data, ok=200):
        """통제 API 응답을 그대로 넘기되, 오류는 정해진 필드만 넘긴다."""
        if status == ok:
            response = jsonify(data)
            response.status_code = status
            return response
        code = data.get('error') if isinstance(data.get('error'), str) else 'api_error'
        extra = {k: data[k] for k in ('detail', 'fields', 'cmd_id', 'sent') if k in data}
        if status in (400, 404, 413, 415):
            raise UiError(status, code, 'invalid_request', **extra)
        if status == 503:
            raise UiError(503, code, code if code in UPSTREAM_503 else 'api_error', **extra)
        log('control_ui_api_bad_response', path=request.path, status=status)
        raise UiError(502, 'api_error', 'api_error')

    # ----- 공통 응답 -----

    @app.after_request
    def secure(response):
        h = response.headers
        h['Content-Security-Policy'] = CSP
        h['X-Frame-Options'] = 'DENY'
        h['Referrer-Policy'] = 'no-referrer'  # Grafana 링크로 넘어갈 때 이 화면 주소를 넘기지 않는다
        h['X-Content-Type-Options'] = 'nosniff'
        h['Cross-Origin-Opener-Policy'] = 'same-origin'
        h['Cache-Control'] = 'no-cache' if request.path.startswith('/static/') else 'no-store'
        # HSTS는 보내지 않는다. 같은 호스트의 Grafana(3000)가 http라서 호스트 전체에 https를 강제하면 Grafana가 막힌다.
        return response

    @app.errorhandler(UiError)
    def ui_error(exc):
        response = jsonify(error=exc.code, **exc.extra)
        response.status_code = exc.status
        if 'retry_after' in exc.extra:
            response.headers['Retry-After'] = str(exc.extra['retry_after'])
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=(exc.name or 'error').lower().replace(' ', '_')), exc.code

    @app.errorhandler(Exception)
    def unexpected(exc):
        log('control_ui_error', error=type(exc).__name__, method=request.method, path=request.path)
        return jsonify(error='internal_error'), 500

    # ----- 화면·상태 확인 -----

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/healthz')
    def healthz():
        # 프로세스가 응답하는지만 본다(인증 없음). 통제 API 연결은 화면의 연결 상태와 로그로 본다.
        return jsonify(status='ok')

    # ----- 로그인 -----

    @app.get('/api/session')
    def get_session():
        return jsonify(session_view(current(False)))

    @app.post('/api/login')
    def login():
        same_site()
        body = body_json()
        user, password = body.get('username'), body.get('password')
        if set(body) != {'username', 'password'} or not isinstance(user, str) or not isinstance(password, str):
            raise UiError(400, 'invalid_request')
        refresh_accounts()
        remote = request.remote_addr
        valid = bool(USER.fullmatch(user))
        # 형식이 틀린 ID는 로그에 남기지 않는다(비밀번호를 ID 칸에 잘못 넣었을 수 있다).
        # 형식이 맞는 ID는 계정이 없어도 똑같이 세고 잠가서 계정이 있는지 드러나지 않게 한다.
        name = user if valid else None
        wait = guard.locked(user) if valid else 0
        if wait:
            metrics.login('locked')
            log('ui_login', user=name, result='locked', remote=remote)
            raise UiError(429, 'locked', retry_after=math.ceil(wait))
        if not accounts.verify(user, password):
            wait = guard.failed(user) if valid else 0
            metrics.login('fail')
            log('ui_login', user=name, result='fail', remote=remote, locked=bool(wait))
            if wait:
                raise UiError(429, 'locked', retry_after=math.ceil(wait))
            raise UiError(401, 'invalid_credentials')
        guard.succeeded(user)
        sessions.drop(request.cookies.get(COOKIE))  # 로그인할 때마다 새 세션 ID(세션 고정 방지)
        session = sessions.create(user)
        metrics.login('success')
        log('ui_login', user=user, result='success', remote=remote)
        response = jsonify(session_view(session))
        response.set_cookie(COOKIE, session.sid, secure=True, httponly=True, samesite='Strict', path='/')
        return response

    @app.post('/api/logout')
    def logout():
        session = current(True)
        sessions.drop(session.sid)
        log('ui_logout', user=session.user, reason='user')
        response = jsonify(status='logged_out')
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    # ----- 조회 -----

    @app.get('/api/status')
    def status():
        """통제 API 연결·DB·MQTT·키 상태. 하나라도 안 되면 화면이 명령 버튼을 막고 "원격 제어 중단"을 띄운다."""
        current(False)
        result = dict(api=False, db=False, mqtt=False, key=None, can_command=False)
        try:
            code, ready = api.call('GET', '/readyz', auth=False)
        except Unreachable as exc:
            log('control_ui_api_unreachable', method='GET', path='/readyz', error=exc.error, sent=exc.sent)
            return jsonify(result)
        if code not in (200, 503) or not isinstance(ready, dict):
            log('control_ui_api_bad_response', method='GET', path='/readyz', status=code)
            return jsonify(result)
        result.update(api=True, db=ready.get('db') is True, mqtt=ready.get('mqtt') is True)
        try:
            code, me = api.call('GET', API + '/me')
        except Unreachable as exc:
            log('control_ui_api_unreachable', method='GET', path=API + '/me', error=exc.error, sent=exc.sent)
            return jsonify(result)
        if code == 200 and isinstance(me, dict):
            result['key'] = me.get('scope') == 'command'  # 조회 전용 키면 명령을 보낼 수 없다
        elif code in (401, 403):
            log('control_ui_api_rejected', method='GET', path=API + '/me', status=code)
            result['key'] = False
        else:
            log('control_ui_api_bad_response', method='GET', path=API + '/me', status=code)
        result['can_command'] = result['db'] and result['mqtt'] and result['key'] is True
        return jsonify(result)

    @app.get('/api/devices')
    def devices():
        current(False)
        status, data = upstream('GET', API + '/devices')
        if status != 200:
            return passed(status, data)
        rows = data.get('devices')
        if not isinstance(rows, list):
            raise UiError(502, 'api_error')
        return jsonify(devices=[device_view(d, cfg['STALE_SEC'], cfg['GRAFANA_DEVICE_URL'])
                                for d in rows if isinstance(d, dict)])

    @app.get('/api/commands')
    def commands():
        current(False)
        args = {'limit': '50'}
        device = request.args.get('device_id')
        if device is not None:
            try:
                identity('site1', device)
            except ValueError:
                raise UiError(400, 'invalid_device_id') from None
            args['device_id'] = device
        return passed(*upstream('GET', API + '/commands?' + urlencode(args)))

    @app.get('/api/commands/<cmd_id>')
    def command(cmd_id):
        current(False)
        if not CMD_ID.fullmatch(cmd_id):
            raise UiError(404, 'not_found')
        return passed(*upstream('GET', API + '/commands/' + cmd_id))

    # ----- 명령 -----

    @app.post('/api/commands')
    def create_command():
        session = current(True)
        fields = dict(user=session.user, remote=request.remote_addr)
        try:
            return issue(session, fields)
        except UiError as exc:
            result = exc.result or 'invalid_request'
            metrics.command(result)
            log('ui_command', **fields, result=result, error=exc.code, cmd_id=exc.extra.get('cmd_id'))
            raise

    def issue(session, fields):
        body = body_json()
        unknown = sorted(set(body) - set(COMMAND_FIELDS))
        if unknown:  # operator는 받지 않는다. 서버가 로그인한 계정으로 붙인다.
            raise UiError(400, 'unknown_field', fields=unknown)
        missing = [k for k in COMMAND_FIELDS if k not in body]
        if missing:
            raise UiError(400, 'missing_field', fields=missing)
        site, device, value = body['site_id'], body['device_id'], body['value']
        try:
            identity(site, device)
        except ValueError as exc:  # invalid_site_id, invalid_device_id, identity_too_long
            raise UiError(400, str(exc)) from None
        fields.update(site_id=site, device_id=device)
        if not number(value):
            raise UiError(400, 'invalid_value', detail='value는 숫자(JSON number)여야 합니다.')
        fields['value'] = value
        wait = limit.hit(session.user)
        if wait:
            raise UiError(429, 'rate_limited', 'rate_limited', retry_after=math.ceil(wait))
        status, data = upstream('POST', API + '/commands',
                                dict(site_id=site, device_id=device, value=value, operator=session.user))
        response = passed(status, data, ok=202)
        metrics.command('sent')
        log('ui_command', **fields, result='sent', cmd_id=data.get('cmd_id'))
        return response

    return app
