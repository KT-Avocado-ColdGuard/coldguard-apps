"""중앙 통제 API (Flask)

- 운영자 명령을 받아 중앙 Mosquitto의 coldguard/<site>/<device>/cmd로 발행한다(QoS 1).
- 명령의 최종 판정(허용 온도·만료·중복·현장 잠금·미등록 장비)은 시설 controller가 한다.
  API는 인증·권한과 형식만 검사한다. 범위 밖 온도도 controller까지 전달되어 차단 기록·지표·알림이 현장에 남는다.
- 명령 요청은 command_request에 먼저 기록한 뒤 발행한다(기록 없이 나간 명령이 없도록).
- 판정 결과는 수집 워커가 저장한 command_ack를 읽어 보여준다. API는 명령을 다시 보내지 않는다.
  시설 연결이 끊긴 동안의 처리는 시설(현장 웹)이 맡는다.
- 조회: 장비 최신 상태, 온도 추이, 이벤트, 명령 이력.

실행
- 배포: gunicorn (deploy 저장소 control-api/deployment.yaml의 command 참고)
- 로컬: python -m control_api.app (Flask 개발 서버, 127.0.0.1:8000)
"""
import atexit
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from common.coldguard_protocol import identity, loads, number, topic
from common.coldguard_runtime import log
from control_api import config as config_module
from control_api.auth import KeyRing, bearer
from control_api.bus import NotConnected, Publisher, PublishUnconfirmed
from control_api.store import Store

API = '/api/v1'
CMD_ID = re.compile(r'[A-Za-z0-9-]{1,50}')
DIGITS = re.compile(r'[0-9]{1,6}')
COMMAND_FIELDS = {'site_id', 'device_id', 'value', 'operator'}
OPERATOR_MAX = 30
MAX_BODY = 4096  # 명령 본문 상한(바이트). 실제 명령은 200바이트 안팎이다.
VALUE_LIMIT = Decimal('999.99')  # DECIMAL(5,2)에 들어가는 범위(저장 형식). 허용 온도 판정은 controller가 한다.
SERIES_POINTS = 720  # 온도 추이를 한 번에 돌려주는 최대 구간 수


class ApiError(Exception):
    def __init__(self, status, code, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


# ---------- 시각 ----------

def ms(dt):
    """DATETIME(3)과 같은 밀리초 단위로 자른다. DB 기록과 MQTT 메시지의 시각을 똑같이 맞추기 위해서다."""
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def wire(dt):
    """UTC + Z 문자열(밀리초). DB에서 읽은 시각(시간대 없음)은 UTC로 본다."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def naive(dt):
    """DB 비교용: 시간대를 떼어 낸 UTC."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def num(value):
    """DB의 DECIMAL(또는 GROUP_CONCAT 문자열)을 JSON 숫자로."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    return float(Decimal(str(value)))


# ---------- 명령 상태 ----------

def command_status(req, acks, now):
    """명령 하나의 상태를 ack 기록으로 정한다.

    - 같은 cmd_id라도 대상 장비와 같은 토픽(site, device)에서 온 ack만 본다.
    - 첫 판정(재전송 거부 duplicate가 아닌 가장 이른 ack)이 결과다. 같은 명령이 다시 들어와
      duplicate로 거부돼도 원래 결과를 덮어쓰지 않고 replay_rejected로 따로 센다.
    - 판정이 없으면 만료 전 pending(응답 대기), 만료 후 no_response(만료·결과 미수신).
      no_response는 끝난 상태가 아니다. 시설 연결이 복구되면 늦게 도착한 ack(보통 expired)로 바뀐다.
    """
    mine = sorted((a for a in acks if a['cmd_id'] == req['cmd_id']
                   and (a['site_id'], a['device_id']) == (req['site_id'], req['device_id'])),
                  key=lambda a: (a['acked_at'], a.get('id', 0)))
    decided = [a for a in mine if a['reason'] != 'duplicate']
    replays = [a for a in mine if a['reason'] == 'duplicate']
    first = decided[0] if decided else (replays[0] if replays else None)
    if first is None:
        status = 'pending' if naive(now) < req['expires_at'] else 'no_response'
        return dict(status=status, reason=None, decided_at=None, replay_rejected=0)
    return dict(status='accepted' if first['result'] == 'accepted' else 'rejected',
                reason=first['reason'], decided_at=wire(first['acked_at']),
                replay_rejected=len(replays) - (0 if decided else 1))


def command_view(req, acks, now):
    return dict(cmd_id=req['cmd_id'], site_id=req['site_id'], device_id=req['device_id'],
                action=req['action'], value=num(req['value']), issued_by=req['issued_by'],
                issued_at=wire(req['issued_at']), expires_at=wire(req['expires_at']),
                **command_status(req, acks, now))


# ---------- 입력 검사 ----------

def device_ok(device):
    try:
        identity('site1', device)  # 장비 ID 규칙(fz + 숫자)은 controller와 같은 공통 규칙을 쓴다.
        return True
    except ValueError:
        return False


def parse_value(value):
    if not number(value):  # bool, 문자열, null, NaN은 거부(JSON 숫자만)
        raise ApiError(400, 'invalid_value', detail='value는 숫자(JSON number)여야 합니다.')
    d = Decimal(str(value))
    if abs(d) > VALUE_LIMIT or d != d.quantize(Decimal('0.01')):
        raise ApiError(400, 'invalid_value', detail='value는 소수 둘째 자리까지, 절댓값 999.99 이하')
    return d


def parse_operator(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, 'invalid_operator')
    value = value.strip()
    if len(value) > OPERATOR_MAX or not value.isprintable():
        raise ApiError(400, 'invalid_operator', detail=f'operator는 제어 문자 없이 {OPERATOR_MAX}자 이하')
    return value


def int_arg(name, default, low, high):
    raw = request.args.get(name)
    if raw is None:
        return default
    if not DIGITS.fullmatch(raw) or not low <= int(raw) <= high:
        raise ApiError(400, 'invalid_' + name, detail=f'{name}는 {low}~{high} 정수')
    return int(raw)


# ---------- 지표 ----------

class Metrics:
    """/metrics는 API 포트와 다른 포트(기본 9204)로 연다. tailnet에 노출되는 API 포트에는 지표를 두지 않는다."""

    def __init__(self, cfg):
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
        r = CollectorRegistry()
        self._req = Counter('coldguard_api_requests_total', '요청 수', ('endpoint', 'method', 'code'), registry=r)
        self._dur = Histogram('coldguard_api_request_duration_seconds', '응답 시간', ('endpoint',), registry=r)
        self._cmd = Counter('coldguard_api_commands_total', '명령 요청 처리 결과', ('result',), registry=r)
        self._auth = Counter('coldguard_api_auth_failures_total', '인증·권한 거부', ('reason',), registry=r)
        self._mqtt = Gauge('coldguard_api_mqtt_up', '중앙 MQTT 연결 여부', registry=r)
        self._db = Gauge('coldguard_api_db_up', '마지막 DB 처리 성공 여부', registry=r)
        try:
            self.server, self.thread = start_http_server(cfg['METRICS_PORT'], addr=cfg['METRICS_HOST'],
                                                         registry=r)
        except OSError as exc:
            # 지표 포트를 못 열어도 명령·조회는 계속한다(지표 수집만 빠진다).
            self.server = self.thread = None
            log('control_api_metrics_unavailable', error=type(exc).__name__, port=cfg['METRICS_PORT'])

    def request(self, endpoint, method, code, seconds):
        self._req.labels(endpoint, method, str(code)).inc()
        self._dur.labels(endpoint).observe(seconds)

    def command(self, result):
        self._cmd.labels(result).inc()

    def auth_failure(self, reason):
        self._auth.labels(reason).inc()

    def mqtt(self, up):
        self._mqtt.set(1 if up else 0)

    def db(self, up):
        self._db.set(1 if up else 0)


# ---------- 앱 ----------

def create_app(cfg=None, store=None, publisher=None, keys=None, metrics=None, clock=None):
    """배포에서는 인자 없이 부른다(gunicorn "control_api.app:create_app()"). 시험에서는 가짜 객체를 넣는다."""
    try:
        cfg = cfg or config_module.load()
        keys = keys or KeyRing.from_file(cfg['API_KEYS_FILE'], cfg['SITES'])
    except (ValueError, OSError) as exc:
        # 설정·키 파일 오류는 기동을 멈춘다. 메시지에 비밀번호나 키 값은 들어가지 않는다.
        log('control_api_config_error', reason=str(exc))
        raise
    metrics = metrics or Metrics(cfg)
    store = store or Store(cfg)
    if publisher is None:
        publisher = Publisher(cfg, on_state=metrics.mqtt)
        publisher.start()
        atexit.register(publisher.stop)
    clock = clock or (lambda: datetime.now(timezone.utc))
    log('control_api_start', client_id=cfg['MQTT_CLIENT_ID'], sites=list(cfg['SITES']),
        cmd_ttl_sec=cfg['CMD_TTL_SEC'], keys=[p.name for p in keys.principals()])

    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_BODY
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    def db(fn, *args):
        try:
            result = fn(*args)
        except Exception as exc:
            metrics.db(False)
            log('control_api_db_error', error=type(exc).__name__)
            raise ApiError(503, 'db_unavailable') from exc
        metrics.db(True)
        return result

    def reject_auth(status, reason, principal=None, **fields):
        metrics.auth_failure(reason)
        log('control_api_auth_rejected', reason=reason, key=principal.name if principal else None,
            method=request.method, path=request.path, remote=request.remote_addr, **fields)
        if status == 401:
            raise ApiError(401, 'unauthorized')
        raise ApiError(403, 'forbidden', detail=fields.pop('detail', None) or '권한이 없습니다.')

    def require(scope):
        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                token = bearer(request.headers.get('Authorization'))
                principal = keys.lookup(token) if token else None
                if principal is None:
                    reject_auth(401, 'missing' if token is None else 'invalid')
                if scope == 'command' and not principal.can_command():
                    reject_auth(403, 'scope', principal, detail='이 키는 조회만 할 수 있습니다.')
                g.principal = principal
                return fn(*args, **kwargs)
            return wrapper
        return deco

    def scoped_sites(site_id):
        """조회·명령 대상 시설. site_id가 없으면 키에 허용된 모든 시설."""
        principal = g.principal
        if site_id is None:
            return principal.sites
        if not isinstance(site_id, str) or site_id not in cfg['SITES']:
            raise ApiError(400, 'unknown_site')
        if site_id not in principal.sites:
            reject_auth(403, 'site', principal, site_id=site_id, detail='이 키로는 해당 시설에 접근할 수 없습니다.')
        return (site_id,)

    def device_arg():
        device = request.args.get('device_id')
        if device is not None and not device_ok(device):
            raise ApiError(400, 'invalid_device_id')
        return device

    @app.before_request
    def started():
        g.started = time.perf_counter()

    @app.after_request
    def finished(response):
        endpoint = request.url_rule.rule if request.url_rule else 'unmatched'
        metrics.request(endpoint, request.method, response.status_code,
                        time.perf_counter() - g.get('started', time.perf_counter()))
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.errorhandler(ApiError)
    def api_error(exc):
        response = jsonify(error=exc.code, **exc.extra)
        response.status_code = exc.status
        if exc.status == 401:
            response.headers['WWW-Authenticate'] = 'Bearer'
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=(exc.name or 'error').lower().replace(' ', '_')), exc.code

    @app.errorhandler(Exception)
    def unexpected(exc):
        log('control_api_error', error=type(exc).__name__, method=request.method, path=request.path)
        return jsonify(error='internal_error'), 500

    # ----- 상태 확인 (인증 없음, 내부 정보는 참·거짓만) -----

    @app.get('/healthz')
    def healthz():
        # 프로세스가 응답하는지만 본다(liveness). 브로커·DB 장애로 파드를 재시작하지 않는다.
        return jsonify(status='ok')

    @app.get('/readyz')
    def readyz():
        # DB가 되어야 조회와 명령 기록이 가능하다. MQTT 상태는 함께 알려 주되 준비 여부에는 넣지 않는다.
        try:
            db(store.ping)
            ok = True
        except ApiError:
            ok = False
        response = jsonify(status='ready' if ok else 'not_ready', db=ok, mqtt=publisher.connected())
        response.status_code = 200 if ok else 503
        return response

    # ----- 키 정보 -----

    @app.get(API + '/me')
    @require('read')
    def me():
        p = g.principal
        return jsonify(name=p.name, scope=p.scope, sites=list(p.sites))

    # ----- 명령 -----

    @app.post(API + '/commands')
    @require('command')
    def create_command():
        try:
            return issue_command()
        except ApiError as exc:
            metrics.command(exc.code)
            raise

    def issue_command():
        principal = g.principal
        if not request.is_json:
            raise ApiError(415, 'json_required')
        data = request.get_data()
        # 길이를 알리지 않는 chunked 본문은 Werkzeug가 상한에서 조용히 잘라 넘긴다. 상한에 닿았으면 잘렸을 수 있으므로
        # 앞부분만 처리하지 않고 거부한다(Content-Length가 상한을 넘는 요청은 Werkzeug가 먼저 413으로 막는다).
        if len(data) >= MAX_BODY:
            raise ApiError(413, 'request_entity_too_large')
        try:
            body = loads(data)  # 중복 키·NaN·객체가 아닌 JSON은 거부(공통 규칙)
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, 'invalid_json') from None
        unknown = sorted(set(body) - COMMAND_FIELDS)
        if unknown:
            raise ApiError(400, 'unknown_field', fields=unknown)
        missing = [k for k in ('site_id', 'device_id', 'value') if k not in body]
        if missing:
            raise ApiError(400, 'missing_field', fields=missing)
        site, device = body['site_id'], body['device_id']
        scoped_sites(site)
        if not isinstance(device, str) or not device_ok(device):
            raise ApiError(400, 'invalid_device_id')
        value = parse_value(body['value'])
        operator = parse_operator(body.get('operator'))
        issued_by = principal.name + (':' + operator if operator else '')

        # 연결이 없으면 기록도 발행도 하지 않는다(나가지 않은 명령이 기록에 남지 않게).
        if not publisher.connected():
            raise ApiError(503, 'mqtt_unavailable', sent=False)
        now = ms(clock())
        expires = ms(now + timedelta(seconds=cfg['CMD_TTL_SEC']))
        cmd_id = str(uuid.uuid4())
        cmd = dict(site_id=site, device_id=device, cmd_id=cmd_id, action='set_temp', value=float(value),
                   issued_at=wire(now), expires_at=wire(expires), issued_by=issued_by)
        row = dict(cmd_id=cmd_id, site_id=site, device_id=device, action='set_temp', value=value,
                   issued_by=issued_by, issued_at=naive(now), expires_at=naive(expires))
        db(store.insert_request, row)
        try:
            publisher.publish(topic(site, device, 'cmd'), cmd)
        except NotConnected:
            log('command_not_sent', cmd_id=cmd_id, site_id=site, device_id=device, reason='mqtt_unavailable')
            raise ApiError(503, 'mqtt_unavailable', cmd_id=cmd_id, sent=False) from None
        except PublishUnconfirmed as exc:
            log('command_publish_unconfirmed', cmd_id=cmd_id, site_id=site, device_id=device, detail=str(exc))
            raise ApiError(503, 'publish_unconfirmed', cmd_id=cmd_id,
                           detail='전달 여부를 결과 조회로 확인하세요. 만료 시각 이후에는 시설에서 적용되지 않습니다.'
                           ) from None
        metrics.command('sent')
        log('command_sent', cmd_id=cmd_id, site_id=site, device_id=device, value=float(value),
            issued_by=issued_by, key=principal.name, expires_at=cmd['expires_at'])
        response = jsonify(command_view(row, [], now))
        response.status_code = 202
        response.headers['Location'] = f'{API}/commands/{cmd_id}'
        return response

    @app.get(API + '/commands/<cmd_id>')
    @require('read')
    def get_command(cmd_id):
        if not CMD_ID.fullmatch(cmd_id):
            raise ApiError(404, 'not_found')
        req = db(store.request, cmd_id, g.principal.sites)
        if req is None:  # 없거나 이 키의 시설이 아닌 명령
            raise ApiError(404, 'not_found')
        return jsonify(command_view(req, db(store.acks, [cmd_id]), clock()))

    @app.get(API + '/commands')
    @require('read')
    def list_commands():
        sites = scoped_sites(request.args.get('site_id'))
        device = device_arg()
        limit = int_arg('limit', 50, 1, 200)
        reqs = db(store.requests, sites, device, limit)
        acks = db(store.acks, [r['cmd_id'] for r in reqs])
        now = clock()
        return jsonify(commands=[command_view(r, acks, now) for r in reqs])

    # ----- 조회 -----

    @app.get(API + '/devices')
    @require('read')
    def devices():
        sites = scoped_sites(request.args.get('site_id'))
        rows = db(store.latest, sites)
        now = naive(clock())
        return jsonify(devices=[dict(
            site_id=r['site_id'], device_id=r['device_id'], seq=r['seq'],
            measured_at=wire(r['measured_at']), received_at=wire(r['received_at']),
            age_sec=round((now - r['measured_at']).total_seconds(), 3),
            temp_c=num(r['temp_c']), setpoint_c=num(r['setpoint_c']), door_open=bool(r['door_open']),
            cooling=bool(r['cooling']), status=r['status']) for r in rows])

    @app.get(API + '/devices/<site_id>/<device_id>/telemetry')
    @require('read')
    def telemetry(site_id, device_id):
        scoped_sites(site_id)
        if not device_ok(device_id):
            raise ApiError(400, 'invalid_device_id')
        minutes = int_arg('minutes', 60, 1, 1440)
        step = max(1, math.ceil(minutes * 60 / SERIES_POINTS))
        since = naive(clock() - timedelta(minutes=minutes))
        rows = db(store.series, site_id, device_id, since, step)
        return jsonify(site_id=site_id, device_id=device_id, minutes=minutes, step_sec=step, points=[dict(
            t=wire(datetime.fromtimestamp(int(r['bucket']) * step, timezone.utc)),
            temp_avg=round(num(r['temp_avg']), 2), temp_min=num(r['temp_min']), temp_max=num(r['temp_max']),
            setpoint_c=num(r['setpoint_c']), door_open=bool(r['door_open']), samples=int(r['samples']))
            for r in rows])

    @app.get(API + '/events')
    @require('read')
    def events():
        sites = scoped_sites(request.args.get('site_id'))
        device = device_arg()
        minutes = int_arg('minutes', 1440, 1, 10080)
        limit = int_arg('limit', 100, 1, 500)
        since = naive(clock() - timedelta(minutes=minutes))
        rows = db(store.events, sites, device, since, limit)
        return jsonify(events=[dict(
            id=r['id'], site_id=r['site_id'], device_id=r['device_id'], event_type=r['event_type'],
            severity=r['severity'], temp_c=num(r['temp_c']), predicted_sec=r['predicted_sec'],
            detail=r['detail'], occurred_at=wire(r['occurred_at']), received_at=wire(r['received_at']))
            for r in rows])

    return app


def main():
    app = create_app()
    app.run(host=os.environ.get('CONTROL_API_HOST', '127.0.0.1'),
            port=int(os.environ.get('CONTROL_API_PORT', '8000')), threaded=True)


if __name__ == '__main__':
    main()
