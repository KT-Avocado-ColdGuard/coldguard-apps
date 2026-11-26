"""
측정값 저장/경보

- 시뮬레이터의 MQTT 측정값을 받아 MySQL에 저장합니다.
- 같은 측정값이 재전송되어도 중복 저장하지 않습니다.
- 온도 상승 추세를 계산해 위험 온도까지 남은 시간을 예측합니다.
- 고온 / 예측 경보 / 데이터 단절을 판단하고 이벤트를 전달합니다.
- 모니터링용 /metrics를 제공합니다.
- 실행: python data_collector.py
"""

import hashlib
import json
import math
import os
import queue
import signal
import threading
from datetime import datetime, timezone
from common.coldguard_protocol import topic, telemetry, loads, parse_time
from common.coldguard_runtime import Database, journal, EventSender, sql_time, mqtt_client, publish

from common.temperature_analysis import analyze

REASONS = {"normal": "정상 운전", "early": "고온 예측 경보",
           "hot": "고온 경보", "stale": "데이터 확인 불가"}


def now():
    return datetime.now(timezone.utc).timestamp()


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def log(event, **fields):
    # Fluent Bit이 읽을 수 있도록 한 줄 JSON으로 출력한다. 비밀번호/원문은 제외한다.
    print(json.dumps({"event": event, "logged_at": iso(now()), **fields},
                     ensure_ascii=False, allow_nan=False), flush=True)


def settings():
    from common.coldguard_runtime import settings as common
    cfg=common('collector')
    cfg['TOPICS']={topic(cfg['SITE'],d,'telemetry'):d for d in cfg['DEVICES']}
    return cfg


def decode(payload, site, device):
    raw=telemetry(loads(payload),topic(site,device,'telemetry'))
    # DATETIME(3)/DECIMAL(5,2)의 실제 저장 정밀도를 넘는 값은 조용히 반올림하지 않는다.
    from decimal import Decimal
    for k in ('temp_c','setpoint_c'):
        n=Decimal(str(raw[k]))
        if abs(n)>Decimal('999.99') or n!=n.quantize(Decimal('0.01')):
            raise ValueError('temperature_storage_precision')
    stamp=parse_time(raw['measured_at'])
    return dict(raw=raw,site=site,device=device,seq=raw['seq'],stamp=raw['measured_at'],
                ts=stamp.timestamp(),temp=float(raw['temp_c']),target=float(raw['setpoint_c']),
                door=raw['door_open'],cooling=raw['cooling'])


class Store(Database):
    # 확정 SQL 세 테이블만 사용한다. 추세 상태는 서비스 영속 파일에 보관한다.
    def __init__(self,cfg):
        super().__init__(cfg)
        self.cfg=cfg
        self.state=journal(cfg['STATE_DB'],'collector',cfg['SITE'])
        self.state.execute('CREATE TABLE IF NOT EXISTS trends(device TEXT PRIMARY KEY,body TEXT)')
        self.state.commit()
        self.sender=EventSender(self,self.state,cfg['SITE'],('early_warning','high_temp','data_lost'))

    def old(self,device):
        row=self.state.execute('SELECT body FROM trends WHERE device=?',(device,)).fetchone()
        return json.loads(row[0]) if row else None

    def remember(self,state):
        with self.state:
            self.state.execute('INSERT OR REPLACE INTO trends VALUES(?,?)',
                               (state['device'],json.dumps(state)))

    @staticmethod
    def alarm(state,stamp):
        kind={'early':'early_warning','hot':'high_temp','stale':'data_lost'}.get(state['status'])
        if not kind:
            return None  # 확정 event_type에는 정상 복귀 항목이 없다.
        eta=state['eta']
        return dict(site_id=state['site'],device_id=state['device'],event_type=kind,
                    severity='critical' if kind=='high_temp' else 'warning',temp_c=state['temp'],
                    predicted_sec=min(2147483647,math.ceil(eta)) if eta is not None else None,
                    detail=REASONS[state['status']],at=iso(stamp))

    def save(self,msg,received,stale_sec):
        state,changed=analyze(self.old(msg['device']),msg,received,stale_sec)
        event=self.alarm(state,received if state['status']=='stale' else msg['ts']) if changed else None
        raw=msg['raw']
        args=(msg['site'],msg['device'],msg['seq'],sql_time(msg['stamp']),msg['temp'],
              msg['target'],msg['door'],msg['cooling'],raw['status'])
        db=self.connect()
        result='stored'
        try:
            with db.cursor(dictionary=True) as cur:
                cur.execute('SELECT * FROM telemetry WHERE site_id=%s AND device_id=%s AND seq=%s',args[:3])
                old=cur.fetchone()
                if old:
                    keys=('site_id','device_id','seq','measured_at','temp_c','setpoint_c','door_open','cooling','status')
                    if tuple(old[k] for k in keys)!=args:
                        # DECIMAL 비교는 문자열 변환을 통해 부동소수점 오차를 제거한다.
                        from decimal import Decimal
                        actual=tuple(old[k] for k in keys)
                        expected=(*args[:4],Decimal(str(args[4])),Decimal(str(args[5])),*args[6:])
                        if actual!=expected:
                            raise ValueError('seq_conflict')
                    result='duplicate'
                else:
                    cur.execute('INSERT INTO telemetry(site_id,device_id,seq,measured_at,temp_c,setpoint_c,'
                                'door_open,cooling,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',args)
                if event:
                    self.insert_event(cur,event)
            db.commit()
            # DB 커밋 직후 종료되어 재전달돼도 같은 분석/이벤트를 복구할 수 있다.
            self.remember(state)
            return result,state,event
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def check_stale(self,site,device,stamp,stale_sec):
        state=self.old(device)
        event=None
        if state and state['status']!='stale' and stamp-state['ts']>stale_sec:
            state.update(status='stale',eta=None,risk=None,history=[])
            # 단절 시각은 마지막 측정 + 기준으로 고정하여 재시도 시 같은 이벤트가 된다.
            event=self.alarm(state,state['ts']+stale_sec)
            self.event(event)
            self.remember(state)
        return state,event


class Metrics:
    def __init__(self, cfg):
        from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server
        self.registry = CollectorRegistry()
        labels = ("site", "device")
        def gauge(name, desc):
            return Gauge("coldguard_freezer_" + name, desc, labels, registry=self.registry)
        self.temp = gauge("temperature_celsius", "마지막 저장 측정 온도")
        self.eta = gauge("time_to_danger_seconds", "예상 시간; 계산 불가 시 NaN")
        self.last = gauge("last_measurement_timestamp_seconds", "마지막 측정 Unix 시각")
        self.fresh = gauge("data_available", "측정 데이터 유효 여부")
        self.alarm = gauge("alarm", "정상 0, 예측 1, 고온 2, 데이터 확인 불가 3")
        # 장비별 문 상태와 냉각 상태를 모니터링에 제공한다.
        self.door = gauge("door_open", "문 상태: 열림 1, 닫힘 0; 확인 불가 시 NaN")
        self.cooling = gauge("cooling", "냉각 상태: 가동 1, 정지 0; 확인 불가 시 NaN")
        self.count = Counter("coldguard_collector_messages_total", "처리 건수", ("result",), registry=self.registry)
        self.db_up = Gauge("coldguard_collector_db_up", "마지막 DB 처리 성공 여부", registry=self.registry)
        self.mqtt_up = Gauge("coldguard_collector_mqtt_up", "MQTT 연결 여부", registry=self.registry)
        self.unsent = Gauge("coldguard_collector_unsent_messages", "아직 MQTT로 발행되지 않은 이벤트 수", ("site",), registry=self.registry)
        self.unsent.labels(cfg["SITE"]).set(float("nan"))
        for device in cfg["DEVICES"]:
            self.update(None, cfg["SITE"], device)
        self.server, self.thread = start_http_server(cfg["METRICS_PORT"], addr=cfg["METRICS_HOST"], registry=self.registry)

    def update(self, state, site, device):
        labels = (site, device)
        valid = state is not None and state["status"] != "stale"
        self.fresh.labels(*labels).set(int(valid))
        self.temp.labels(*labels).set(state["temp"] if state else float("nan"))
        self.last.labels(*labels).set(state["ts"] if state else 0)
        self.eta.labels(*labels).set(state["eta"] if valid and state["eta"] is not None else float("nan"))
        self.alarm.labels(*labels).set({"normal": 0, "early": 1, "hot": 2, "stale": 3}[state["status"]] if state else 3)

        # telemetry의 door_open은 내부에서 door라는 이름으로 사용한다.
        # 데이터가 없거나 오래됐으면 확인 불가를 뜻하는 NaN을 표시한다.
        self.door.labels(*labels).set(
            int(state["door"]) if valid else float("nan")
        )
        self.cooling.labels(*labels).set(
            int(state["cooling"]) if valid else float("nan")
        )


def main():
    try:
        cfg = settings()
    except ValueError as exc:
        log("config_error", reason=str(exc))
        return 2
    stop, overloaded = threading.Event(), threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *args: stop.set())
    inbox = queue.Queue(maxsize=1000)
    store, metrics = Store(cfg), Metrics(cfg)
    client = mqtt_client(cfg)

    def connected(client, userdata, flags, reason, properties):
        metrics.mqtt_up.set(0 if reason.is_failure else 1)
        if not reason.is_failure:
            client.subscribe([(t, 1) for t in cfg["TOPICS"]])
        log("mqtt_connect", reason=str(reason))

    def disconnected(client, userdata, flags, reason, properties):
        metrics.mqtt_up.set(0)
        log("mqtt_disconnect", reason=str(reason))

    def message(client, userdata, msg):
        try:
            inbox.put_nowait(msg)
        except queue.Full:
            overloaded.set()  # 미확인 메시지는 영속 세션에서 서비스 재시작 후 재전달한다.

    client.on_connect_fail = lambda c, u: log("mqtt_connect_failed")
    client.on_connect, client.on_disconnect, client.on_message = connected, disconnected, message
    from common.coldguard_runtime import watch_subscriptions
    watch_subscriptions(client,cfg,'collector',len(cfg['TOPICS']),overloaded.set)
    client.connect_async(cfg["MQTT_HOST"], cfg["MQTT_PORT"], keepalive=60)
    client.loop_start()
    pending = None
    next_check = 0.0
    try:
        while not stop.is_set() and not overloaded.is_set():
            if pending is None:
                try:
                    pending = inbox.get(timeout=0.2)
                except queue.Empty:
                    pass
            try:
                if pending is not None:
                    try:
                        if pending.qos != 1 or pending.retain or pending.topic not in cfg["TOPICS"]:
                            raise ValueError("invalid_delivery")
                        msg = decode(pending.payload, cfg["SITE"], cfg["TOPICS"][pending.topic])
                        if msg["ts"] > now() + cfg["STALE_SEC"]:
                            raise ValueError("future_stamp")
                        result, state, event = store.save(msg, now(), cfg["STALE_SEC"])
                        metrics.db_up.set(1)
                        metrics.count.labels(result).inc()
                        if state:
                            metrics.update(state, msg["site"], msg["device"])
                        if event:
                            log("state_changed", **event)
                    except (ValueError, UnicodeError) as exc:
                        metrics.count.labels("rejected").inc()
                        log("message_rejected", reason=str(exc))
                    # 저장 성공/기존 동일 데이터 확인 또는 잘못된 메시지 거부 후에만 확인한다.
                    client.ack(pending.mid, pending.qos)
                    pending = None
                if now() >= next_check:
                    for device in cfg["DEVICES"]:
                        state, event = store.check_stale(cfg["SITE"], device, now(), cfg["STALE_SEC"])
                        metrics.update(state, cfg["SITE"], device)
                        if event:
                            log("state_changed", **event)
                    metrics.db_up.set(1)
                    # 미전송 건수는 event_log COUNT 쿼리(매번 새 DB 연결)라 1초에 한 번만 센다.
                    # 메시지마다 세면 처리가 밀려 측정값이 stale로 판정된다(11/26 시설 배포에서 확인).
                    try:
                        metrics.unsent.labels(cfg["SITE"]).set(
                            store.sender.pending_count()
                        )
                    except Exception as exc:
                        metrics.unsent.labels(cfg['SITE']).set(float('nan'))
                        log('unsent_count_error',error=type(exc).__name__)
                    next_check = now() + 1
                if client.is_connected():
                    try:
                        store.sender.flush(lambda t,b:publish(client,t,b))
                    except Exception as exc:
                        log('event_publish_retry',error=type(exc).__name__)

            except Exception as exc:
                # 원문/계정이 예외 메시지에 포함될 수 있어 예외 종류만 출력한다.
                metrics.db_up.set(0)
                for device in cfg["DEVICES"]:
                    metrics.update(None, cfg["SITE"], device)
                log("storage_retry", error=type(exc).__name__)
                stop.wait(2)  # pending을 유지한다. DB가 돌아온 후 같은 메시지를 재처리한다.
        return 1 if overloaded.is_set() else 0
    finally:
        client.disconnect()
        client.loop_stop()
        metrics.server.shutdown()
        metrics.server.server_close()
        store.state.close()


if __name__ == "__main__":
    raise SystemExit(main())
