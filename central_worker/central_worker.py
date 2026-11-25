"""
중앙 수집 워커

- 시설 브리지가 중앙 MQTT로 올린 telemetry / event / ack를 공유 구독으로 받습니다.
- 시설과 같은 공통 규칙(common)으로 메시지를 검사한 뒤 중앙 MySQL에 저장합니다.
- DB 저장(또는 같은 데이터가 이미 있음을 확인)한 뒤에만 MQTT 수신 확인을 보냅니다.
- 같은 메시지가 다시 와도 테이블의 유니크 키로 한 번만 저장합니다.
- 같은 키로 내용이 다른 데이터가 오면 저장하지 않고 duplicate_conflict로 기록합니다.
  (예: 시뮬레이터 SIM_DB 초기화로 seq가 1부터 다시 시작된 경우)
- 모니터링용 /metrics를 제공합니다.
- 여러 개(HPA 2~4)로 실행합니다. 파드마다 클라이언트 ID가 다르고(StatefulSet 파드 이름),
  기본 개수 밖의 파드(늘어났다 줄어드는 파드)는 종료 전에 구독을 해제하고 받은 메시지를 모두 처리합니다.
- 회수 모드(DRAIN_ONLY=true): 지정한 파드 ID의 세션에 남은 메시지만 저장하고 끝냅니다.
  DB 장애 중 HPA가 줄인 파드가 메시지를 다 처리하지 못하고 끝났을 때(drain_timeout) 씁니다.
- 실행: python -m central_worker.central_worker
"""
import json
import os
import queue
import re
import signal
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from common.coldguard_protocol import (EVENTS, REASONS, loads, number, parse_time,
                                       split_topic, telemetry)
from common.coldguard_runtime import Database, sql_time

CHANNELS = ('telemetry', 'event', 'ack')
SHARE_GROUP = 'central-worker'
SEVERITIES = ('info', 'warning', 'critical')
RESULTS = ('accepted', 'rejected')
DUPLICATE_KEY = 1062  # MySQL ER_DUP_ENTRY


def now():
    return datetime.now(timezone.utc).timestamp()


def log(event, **fields):
    # 수집기와 같은 한 줄 JSON. 비밀번호와 메시지 원문은 남기지 않는다.
    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    print(json.dumps({'event': event, 'logged_at': stamp, **fields},
                     ensure_ascii=False, allow_nan=False, default=str), flush=True)


def settings(env=None):
    # 시설 서비스의 settings()는 시설 하나를 전제로 하므로 워커는 환경변수만 읽는다.
    env = os.environ if env is None else env
    cfg = dict(
        MQTT_HOST=env.get('MQTT_HOST', 'mosquitto'),
        MQTT_PORT=env.get('MQTT_PORT', '1883'),
        MQTT_USER=env.get('MQTT_USER', 'central-worker'),
        MQTT_PASS=env.get('MQTT_PASS', ''),
        MQTT_CLIENT_ID=env.get('MQTT_CLIENT_ID', 'central-worker'),
        MYSQL_HOST=env.get('MYSQL_HOST', 'mysql'),
        MYSQL_PORT=env.get('MYSQL_PORT', '3306'),
        MYSQL_DB=env.get('MYSQL_DB', 'coldguard_central'),
        MYSQL_USER=env.get('MYSQL_USER', 'coldguard_worker'),
        MYSQL_PASSWORD=env.get('MYSQL_PASSWORD', ''),
        METRICS_HOST=env.get('METRICS_HOST', '0.0.0.0'),
        METRICS_PORT=env.get('METRICS_PORT', '9203'),
        FUTURE_SKEW_SEC=env.get('FUTURE_SKEW_SEC', '300'),
        STABLE_REPLICAS=env.get('STABLE_REPLICAS', '2'),
        DRAIN_SEC=env.get('DRAIN_SEC', '40'),
        DRAIN_ONLY=env.get('DRAIN_ONLY', 'false'),
    )
    missing = [k for k, v in cfg.items() if not str(v).strip()]
    if missing:
        raise ValueError('설정 누락 : ' + ', '.join(missing))
    for k in ('MQTT_PORT', 'MYSQL_PORT', 'METRICS_PORT'):
        cfg[k] = int(cfg[k])
        if not 1 <= cfg[k] <= 65535:
            raise ValueError(k + ' 범위 오류')
    cfg['FUTURE_SKEW_SEC'] = float(cfg['FUTURE_SKEW_SEC'])
    if not cfg['FUTURE_SKEW_SEC'] > 0:
        raise ValueError('FUTURE_SKEW_SEC는 0보다 커야 합니다.')
    cfg['STABLE_REPLICAS'] = int(cfg['STABLE_REPLICAS'])
    cfg['DRAIN_SEC'] = float(cfg['DRAIN_SEC'])
    if cfg['STABLE_REPLICAS'] < 1 or not cfg['DRAIN_SEC'] > 0:
        raise ValueError('STABLE_REPLICAS는 1 이상, DRAIN_SEC는 0보다 커야 합니다.')
    flag = str(cfg['DRAIN_ONLY']).strip().lower()
    if flag not in ('true', 'false'):
        raise ValueError('DRAIN_ONLY는 true 또는 false로 지정하세요.')
    cfg['DRAIN_ONLY'] = flag == 'true'
    if cfg['DRAIN_ONLY'] and 'MQTT_CLIENT_ID' not in env:
        # 회수할 세션을 반드시 명시한다(예: central-worker-3). 기본값으로 엉뚱한 세션을 건드리지 않게 한다.
        raise ValueError('회수 모드는 MQTT_CLIENT_ID에 회수할 파드 이름을 지정해야 합니다.')
    cfg['DRAIN_ON_STOP'] = drain_on_stop(cfg['MQTT_CLIENT_ID'], cfg['STABLE_REPLICAS'])
    cfg['TOPICS'] = [f'$share/{SHARE_GROUP}/coldguard/+/+/{c}' for c in CHANNELS]
    return cfg


def drain_on_stop(client_id, stable_replicas):
    """종료할 때 구독을 해제하고 비우고 끝낼 파드인지 정한다.

    StatefulSet 파드 이름(central-worker-0, -1, ...)이 클라이언트 ID다.
    - 번호 < STABLE_REPLICAS(HPA 최소 개수): 항상 다시 돌아오는 파드다. 구독을 유지한 채 끝내서,
      재시작이나 야간 중지 동안 들어온 메시지를 브로커가 이 파드 몫으로 보관하게 한다.
    - 번호 >= STABLE_REPLICAS: HPA가 줄이면 돌아오지 않을 수 있다. 구독을 해제하고 이미 받은
      메시지를 모두 저장한 뒤 끝내서, 이 파드 몫으로 메시지가 묶이지 않게 한다.
    번호가 없는 이름(로컬 실행)은 구독을 유지한다.
    """
    match = re.search(r'-(\d+)$', client_id)
    return bool(match) and int(match.group(1)) >= stable_replicas


# ---------- 메시지 검사: 테이블 한 줄(row)로 바꾼다 ----------

def storage_temp(value):
    # DECIMAL(5,2)에 그대로 들어가는 값만 받는다. 조용히 반올림하지 않는다.
    if not number(value):
        raise ValueError('invalid_temperature')
    n = Decimal(str(value))
    if abs(n) > Decimal('999.99') or n != n.quantize(Decimal('0.01')):
        raise ValueError('temperature_storage_precision')
    return n


def identity_from(source, channel):
    site, device, got = split_topic(source)
    if got != channel:
        raise ValueError('wrong_channel')
    return site, device


def decode_telemetry(data, source):
    raw = telemetry(data, source)  # 필드 / 토픽 일치 / seq / 시각 / status 검사
    row = (raw['site_id'], raw['device_id'], raw['seq'], sql_time(raw['measured_at']),
           storage_temp(raw['temp_c']), storage_temp(raw['setpoint_c']),
           raw['door_open'], raw['cooling'], raw['status'])
    return row, raw['measured_at']


def decode_event(data, source):
    site, device = identity_from(source, 'event')
    if (data.get('site_id'), data.get('device_id')) != (site, device):
        raise ValueError('topic_identity_mismatch')
    if data.get('event_type') not in EVENTS:
        raise ValueError('invalid_event_type')
    if data.get('severity') not in SEVERITIES:
        raise ValueError('invalid_severity')
    temp = data.get('temp_c')
    temp = None if temp is None else storage_temp(temp)
    predicted = data.get('predicted_sec')
    if predicted is not None and (type(predicted) is not int or not 0 <= predicted <= 2147483647):
        raise ValueError('invalid_predicted_sec')
    detail = data.get('detail')
    if detail is not None and (not isinstance(detail, str) or len(detail) > 255):
        raise ValueError('invalid_detail')
    at = data.get('at')
    row = (site, device, data['event_type'], data['severity'], temp, predicted, detail,
           sql_time(parse_time(at)))
    return row, at


def decode_ack(data, source):
    # ack에는 site_id / device_id가 없으므로 토픽에서 가져온다.
    site, device = identity_from(source, 'ack')
    cmd_id = data.get('cmd_id')
    if not isinstance(cmd_id, str) or not 1 <= len(cmd_id) <= 50:
        raise ValueError('invalid_cmd_id')
    result, reason = data.get('result'), data.get('reason')
    if result not in RESULTS:
        raise ValueError('invalid_result')
    if result == 'accepted' and reason is not None:
        raise ValueError('invalid_reason')
    if result == 'rejected' and reason not in REASONS:
        raise ValueError('invalid_reason')
    at = data.get('at')
    row = (cmd_id, site, device, result, reason, sql_time(parse_time(at)))
    return row, at


DECODERS = {'telemetry': decode_telemetry, 'event': decode_event, 'ack': decode_ack}


def handle(store, source, payload, qos, retain, received, skew):
    """메시지 하나를 검사하고 저장한다. 잘못된 메시지는 ValueError로 알린다."""
    if qos != 1 or retain:
        raise ValueError('invalid_delivery')
    channel = split_topic(source)[2]
    if channel not in DECODERS:
        raise ValueError('invalid_channel')
    try:
        row, stamp = DECODERS[channel](loads(payload), source)
    except (TypeError, KeyError, RecursionError) as exc:
        # 값의 형식이 엉뚱한 메시지(예: 문자열 자리에 목록)는 DB 오류와 구분해 거부한다.
        # 여기서 거부하지 않으면 DB 오류로 오인해 같은 메시지를 끝없이 재시도한다.
        raise ValueError('invalid_message') from exc
    if parse_time(stamp).timestamp() > received + skew:
        raise ValueError('future_stamp')
    return channel, row, store.save(channel, row)


# ---------- 저장 ----------

TABLES = {
    # channel: (INSERT, 중복일 때 기존 행 조회, 조회 조건으로 쓸 row 위치)
    'telemetry': (
        'INSERT INTO telemetry(site_id,device_id,seq,measured_at,temp_c,setpoint_c,'
        'door_open,cooling,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        'SELECT site_id,device_id,seq,measured_at,temp_c,setpoint_c,door_open,cooling,status '
        'FROM telemetry WHERE site_id=%s AND device_id=%s AND seq=%s',
        (0, 1, 2)),
    'event': (
        'INSERT INTO event_log(site_id,device_id,event_type,severity,temp_c,predicted_sec,'
        'detail,occurred_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
        'SELECT site_id,device_id,event_type,severity,temp_c,predicted_sec,detail,occurred_at '
        'FROM event_log WHERE site_id=%s AND device_id=%s AND event_type=%s AND occurred_at=%s',
        (0, 1, 2, 7)),
    'ack': (
        'INSERT INTO command_ack(cmd_id,site_id,device_id,result,reason,acked_at) '
        'VALUES(%s,%s,%s,%s,%s,%s)',
        'SELECT cmd_id,site_id,device_id,result,reason,acked_at '
        'FROM command_ack WHERE cmd_id=%s AND result=%s AND acked_at=%s',
        (0, 3, 5)),
}


def same_row(old, new):
    # DB는 BOOLEAN을 0/1, DECIMAL을 Decimal로 돌려준다. 형식 차이만 맞춰 비교한다.
    def norm(v):
        return int(v) if isinstance(v, bool) else v
    return tuple(norm(v) for v in old) == tuple(norm(v) for v in new)


class Store(Database):
    """연결 하나를 유지하며 쓴다. 오류가 나면 끊고 다음 시도에서 다시 연결한다."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.db = None

    def connection(self):
        if self.db is None or not self.db.is_connected():
            self.close()
            self.db = self.connect()
        return self.db

    def close(self):
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                pass
            self.db = None

    def save(self, channel, row):
        insert, select, keys = TABLES[channel]
        db = self.connection()
        try:
            cur = db.cursor()
            try:
                try:
                    cur.execute(insert, row)
                    db.commit()
                    return 'stored'
                except Exception as exc:
                    if getattr(exc, 'errno', None) != DUPLICATE_KEY:
                        raise
                    db.rollback()
                cur.execute(select, tuple(row[i] for i in keys))
                old = cur.fetchone()
                db.rollback()  # 읽기 트랜잭션 정리
                if old is None:
                    raise RuntimeError('duplicate_row_missing')
                if not same_row(old, row):
                    # 같은 키인데 내용이 다르다: seq 초기화 등 시설 쪽 이상. 저장하지 않고 알린다.
                    log('duplicate_conflict', channel=channel, key=[str(row[i]) for i in keys])
                    raise ValueError('duplicate_conflict')
                return 'duplicate'
            finally:
                cur.close()
        except ValueError:
            raise
        except Exception:
            self.close()
            raise


# ---------- MQTT / 지표 ----------

def mqtt_client(cfg):
    # 클러스터 내부 1883(평문, ClusterIP 전용)으로 붙는다. TLS는 시설 브리지용 8883에서 처리한다.
    # 고정 클라이언트 ID + clean_session=False: 워커가 재시작돼도 브로커가 메시지를 보관한다.
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg['MQTT_CLIENT_ID'],
                         clean_session=False, manual_ack=True)
    client.username_pw_set(cfg['MQTT_USER'], cfg['MQTT_PASS'])
    client.connect_timeout = 5
    client.reconnect_delay_set(1, 30)
    return client


class Metrics:
    def __init__(self, cfg):
        from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server
        self.registry = CollectorRegistry()
        self.count = Counter('coldguard_central_messages_total', '처리 건수',
                             ('channel', 'result'), registry=self.registry)
        self.db_up = Gauge('coldguard_central_db_up', '마지막 DB 처리 성공 여부', registry=self.registry)
        self.mqtt_up = Gauge('coldguard_central_mqtt_up', 'MQTT 연결 여부', registry=self.registry)
        self.server, self.thread = start_http_server(cfg['METRICS_PORT'], addr=cfg['METRICS_HOST'],
                                                     registry=self.registry)


def main():
    try:
        cfg = settings()
    except ValueError as exc:
        log('config_error', reason=str(exc))
        return 2
    stop, overloaded = threading.Event(), threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *args: stop.set())
    inbox = queue.Queue(maxsize=1000)
    store, metrics = Store(cfg), Metrics(cfg)
    client = mqtt_client(cfg)

    # draining: 새 메시지를 받지 않고 남은 메시지만 처리하는 중
    # last: 마지막으로 메시지를 꺼낸(또는 접속한) 시각. 3초간 조용하면 다 비운 것으로 본다.
    state = {'draining': cfg['DRAIN_ONLY'], 'connected': False, 'last': now()}
    deadline = now() + cfg['DRAIN_SEC'] if cfg['DRAIN_ONLY'] else 0.0
    if cfg['DRAIN_ONLY']:
        log('recover_start', client_id=cfg['MQTT_CLIENT_ID'])

    def connected(c, userdata, flags, reason, properties):
        metrics.mqtt_up.set(0 if reason.is_failure else 1)
        if not reason.is_failure:
            if not state['draining']:
                c.subscribe([(t, 1) for t in cfg['TOPICS']])
            elif cfg['DRAIN_ONLY']:
                # 회수 모드: 세션에 남은 구독도 해제해 새 메시지가 더 쌓이지 않게 하고,
                # 브로커가 보관하던 메시지만 받는다.
                c.unsubscribe(cfg['TOPICS'])
            state['connected'], state['last'] = True, now()
        log('mqtt_connect', reason=str(reason), client_id=cfg['MQTT_CLIENT_ID'])

    def subscribed(c, userdata, mid, reasons, properties):
        ok = len(reasons) == len(cfg['TOPICS']) and all(
            not r.is_failure and r.value == 1 for r in reasons)
        log('mqtt_subscription_result', success=ok, codes=[r.value for r in reasons])
        if not ok:
            overloaded.set()  # 구독이 거부되면 재시작해서 다시 시도한다.

    def disconnected(c, userdata, flags, reason, properties):
        metrics.mqtt_up.set(0)
        state['connected'] = False
        log('mqtt_disconnect', reason=str(reason))

    def message(c, userdata, msg):
        try:
            inbox.put_nowait(msg)
        except queue.Full:
            overloaded.set()  # 확인하지 않은 메시지는 영속 세션에서 재시작 후 다시 받는다.

    client.on_connect, client.on_subscribe = connected, subscribed
    client.on_disconnect, client.on_message = disconnected, message
    client.on_connect_fail = lambda c, u: log('mqtt_connect_failed')
    client.connect_async(cfg['MQTT_HOST'], cfg['MQTT_PORT'], keepalive=60)
    client.loop_start()
    pending = None
    drain_failed = False
    try:
        while not overloaded.is_set():
            if stop.is_set() and not state['draining']:
                if not cfg['DRAIN_ON_STOP']:
                    break  # 기본 파드: 구독을 유지한 채 끝낸다. 확인 안 한 메시지는 재시작 후 다시 받는다.
                # 늘어났던 파드: 새 메시지를 그만 받고, 이미 받은 메시지를 모두 처리한 뒤 끝낸다.
                state['draining'], state['last'] = True, now()
                client.unsubscribe(cfg['TOPICS'])
                deadline = now() + cfg['DRAIN_SEC']
                log('drain_start', client_id=cfg['MQTT_CLIENT_ID'])
            if state['draining']:
                # 회수 모드는 접속해서 보관 메시지를 받기 시작한 뒤부터 조용한 시간을 잰다.
                ready = state['connected'] or not cfg['DRAIN_ONLY']
                if pending is None and inbox.empty() and ready and now() - state['last'] >= 3:
                    # 3초간 새 메시지가 없으면 다 비운 것으로 본다.
                    # connected=False면 구독 해제가 브로커에 닿지 않았을 수 있다(세션에 구독이 남음).
                    log('drain_done', client_id=cfg['MQTT_CLIENT_ID'], connected=state['connected'])
                    break
                if now() >= deadline or (cfg['DRAIN_ONLY'] and stop.is_set()):
                    # 마감까지 다 비우지 못했다(메시지가 계속 오거나 DB 장애). 정상 종료로 보고하지 않는다.
                    drain_failed = True
                    log('drain_timeout', client_id=cfg['MQTT_CLIENT_ID'],
                        pending=pending is not None, queued=inbox.qsize())
                    break
            if pending is None:
                try:
                    pending = inbox.get(timeout=0.2)
                    state['last'] = now()
                except queue.Empty:
                    continue
            try:
                try:
                    channel, row, result = handle(store, pending.topic, pending.payload, pending.qos,
                                                  pending.retain, now(), cfg['FUTURE_SKEW_SEC'])
                    metrics.count.labels(channel, result).inc()
                    metrics.db_up.set(1)
                except (ValueError, UnicodeError) as exc:
                    channel = pending.topic.rsplit('/', 1)[-1]
                    metrics.count.labels(channel if channel in CHANNELS else 'unknown', 'rejected').inc()
                    log('message_rejected', topic=pending.topic, reason=str(exc))
                # 저장 / 같은 데이터 확인 / 잘못된 메시지 거부 후에만 확인한다.
                client.ack(pending.mid, pending.qos)
                pending = None
            except Exception as exc:
                # DB 오류: 확인하지 않고 쥐고 있다가 같은 메시지를 다시 처리한다.
                metrics.db_up.set(0)
                log('storage_retry', error=type(exc).__name__)
                if state['draining']:
                    time.sleep(2)  # 종료 처리 중에도 같은 메시지를 다시 시도한다.
                else:
                    # 종료 신호가 오면 대기를 끝내고 다음 반복으로 넘어간다.
                    # 다음 반복에서 파드 종류(기본/늘어난 파드)에 맞는 종료 처리를 한다.
                    stop.wait(2)
        return 1 if overloaded.is_set() or drain_failed else 0
    finally:
        client.disconnect()
        client.loop_stop()
        metrics.server.shutdown()
        metrics.server.server_close()
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
