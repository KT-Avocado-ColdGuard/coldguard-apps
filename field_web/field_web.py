"""
현장 관리 웹 서버

- 현장 담당자가 냉동고 상태와 기록을 확인하는 프로그램입니다.
- MySQL에서 측정값 / 경보 / 명령 처리 기록을 읽습니다.
- 웹에서 누른 문 열기/닫기와 현장 잠금 요청을 MQTT로 전달합니다.
- 허용된 접속 IP와 웹 요청을 검사합니다.
- 실행: python field_web.py
"""
import ipaddress
import json
import math
import os
from common.coldguard_config import values as config_values
from pathlib import Path
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from common.coldguard_protocol import topic, split_topic, loads, delivery, utc
from common.coldguard_runtime import Database, wire_time, settings as common

from flask import Flask, abort, jsonify, render_template, request, session, send_from_directory

ROOT = Path(__file__).resolve().parent


def log(event, **fields):
    print(json.dumps(dict(event=event, stamp=utc(), **fields),
                     ensure_ascii=False), flush=True)


def settings(env=None):
    # 실제 주소/권한/시간 정책은 coldguard_config.py에서 지정한다.
    env = config_values('web') if env is None else env
    cfg=common('web',env)
    cfg.update(WEB_HOST=env.get('WEB_HOST','0.0.0.0'),WEB_PORT=int(env.get('WEB_PORT','8080')),
               WEB_SECRET=env.get('WEB_SECRET',''),CONTROL=env.get('WEB_CONTROL','0')=='1',
               MQTT=env.get('WEB_MQTT','1')=='1')
    if len(cfg['WEB_SECRET'])<32:
        raise ValueError('WEB_SECRET은 32자 이상 필요합니다.')
    if not 1<=cfg['WEB_PORT']<=65535:
        raise ValueError('WEB_PORT 범위 오류')
    if not env.get('WEB_ALLOW_NETS'):
        raise ValueError('WEB_ALLOW_NETS 누락')
    cfg['WEB_ALLOW_NETS']=[ipaddress.ip_network(n.strip(),strict=False) for n in env['WEB_ALLOW_NETS'].split(',')]
    if cfg['CONTROL'] and not cfg['MQTT']:
        raise ValueError('제어하려면 WEB_MQTT=1이 필요합니다.')
    cfg['TOPICS']=[topic(cfg['SITE'],d,ch) for d in cfg['DEVICES'] for ch in ('telemetry','ack','event')]
    return cfg


class Store(Database):
    # 화면 조회에는 SELECT만 사용한다. 명령/잠금 변경은 MQTT로 요청한다.
    def state(self,device):
        from common.temperature_analysis import analyze
        rows=self.query('SELECT * FROM telemetry WHERE site_id=%s AND device_id=%s '
                        'ORDER BY measured_at DESC,seq DESC LIMIT 180',(self.cfg['SITE'],device))
        state=None
        for r in reversed(rows):
            stamp=wire_time(r['measured_at'])
            msg=dict(site=r['site_id'],device=device,temp=float(r['temp_c']),target=float(r['setpoint_c']),
                     door=bool(r['door_open']),cooling=bool(r['cooling']),stamp=stamp,
                     ts=r['measured_at'].replace(tzinfo=timezone.utc).timestamp())
            state,_=analyze(state,msg,msg['ts'],self.cfg['STALE_SEC'])
        if state and state['door'] and state['temp']<=-25 and state['status']!='stale':
            # 장시간 유지된 예측 경보도 collector의 이벤트에서 확인한다.
            events=self.query("SELECT * FROM event_log WHERE site_id=%s AND device_id=%s "
                              "AND event_type IN ('early_warning','high_temp','data_lost') "
                              'ORDER BY occurred_at DESC,id DESC LIMIT 1',(self.cfg['SITE'],device))
            closed=self.query('SELECT measured_at FROM telemetry WHERE site_id=%s AND device_id=%s '
                              'AND door_open=0 ORDER BY measured_at DESC LIMIT 1',(self.cfg['SITE'],device))
            if events and events[0]['event_type']=='early_warning' and (not closed or
                    events[0]['occurred_at']>closed[0]['measured_at']):
                state['status']='early'
        return state

    def locked(self,device):
        rows=self.query("SELECT event_type FROM event_log WHERE site_id=%s AND device_id=%s "
                        "AND event_type IN ('lock_on','lock_off') ORDER BY occurred_at DESC,id DESC LIMIT 1",
                        (self.cfg['SITE'],device))
        return rows[0]['event_type']=='lock_on' if rows else False

    def commands(self,device,limit=30):
        rows=self.query('SELECT * FROM command_log WHERE site_id=%s AND device_id=%s '
                        'ORDER BY decided_at DESC,id DESC LIMIT %s',(self.cfg['SITE'],device,limit))
        return [dict(cmd_id=r['cmd_id'],status=r['result'],
                     request=dict(target=float(r['value']) if r['value'] is not None else None),
                     result=dict(reason=r['reason'],stamp=wire_time(r['decided_at']))) for r in rows]

    def logs(self,device,kind,limit):
        args=(self.cfg['SITE'],device,limit)
        if kind=='measurements':
            rows=self.query('SELECT * FROM telemetry WHERE site_id=%s AND device_id=%s '
                            'ORDER BY measured_at DESC,seq DESC LIMIT %s',args)
            return [dict(seq=r['seq'],stamp=wire_time(r['measured_at']),temp=float(r['temp_c']),
                         target=float(r['setpoint_c']),door=bool(r['door_open'])) for r in rows]
        if kind=='alarms':
            rows=self.query('SELECT * FROM event_log WHERE site_id=%s AND device_id=%s '
                            'ORDER BY occurred_at DESC,id DESC LIMIT %s',args)
            return [dict(stamp=wire_time(r['occurred_at']),reason=r['detail'],
                         temp=float(r['temp_c']) if r['temp_c'] is not None else None,eta=r['predicted_sec']) for r in rows]
        return [dict(stamp=r['result']['stamp'],cmd_id=r['cmd_id'],status=r['status'],
                     target=r['request']['target'],reason=r['result']['reason']) for r in self.commands(device,limit)]


class Broker:
    def __init__(self, cfg):
        import paho.mqtt.client as mqtt
        self.cfg = cfg
        self.lock = threading.Lock()
        self.live = False
        self.subscribed = False
        self.last = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg['MQTT_CLIENT_ID'],
                                  clean_session=True)
        self.client.username_pw_set(cfg['MQTT_USER'],cfg['MQTT_PASS'])
        self.client.tls_set(ca_certs=cfg['MQTT_CA'])
        self.client.connect_timeout = 5
        self.client.reconnect_delay_set(1,30)
        self.client.max_queued_messages_set(10)
        self.client.on_connect = self.connected
        self.client.on_disconnect = self.disconnected
        self.client.on_subscribe = self.subscribe_result
        self.client.on_message = self.message
        self.client.on_connect_fail = lambda c,u: log('field_mqtt_connect_failed')

    def connected(self, client, userdata, flags, reason, properties):
        with self.lock:
            self.live = not reason.is_failure
            self.subscribed = False
        if not reason.is_failure:
            client.subscribe([(t,1) for t in self.cfg['TOPICS']])
        log('field_mqtt_connect', reason=str(reason))

    def subscribe_result(self, client, userdata, mid, reasons, properties):
        with self.lock:
            self.subscribed = len(reasons)==len(self.cfg['TOPICS']) and all(not r.is_failure and r.value==1 for r in reasons)
        log('field_mqtt_subscribe', success=self.subscribed)

    def disconnected(self, client, userdata, flags, reason, properties):
        log("field_mqtt_disconnected",site_id=self.cfg["SITE"],reason=str(reason))
        with self.lock:
            self.live = self.subscribed = False

    def message(self, client, userdata, msg):
        # 현재 상태와 이력은 DB를 기준으로 표시한다. MQTT는 구독 연결을 확인한다.
        try:
            if msg.topic not in self.cfg['TOPICS']:
                return
            site,device,channel=split_topic(msg.topic)
            delivery(channel,msg.qos,msg.retain)
            body=loads(msg.payload)
            if channel!='ack' and (body.get('site_id'),body.get('device_id'))!=(site,device):
                return
            with self.lock:
                self.last=utc()
        except (ValueError,TypeError,UnicodeError):
            log('field_mqtt_message_rejected')

    def status(self):
        with self.lock:
            return dict(connected=self.live, subscribed=self.subscribed, last=self.last)

    def publish(self, topic, body, retain=False):
        if not self.client.is_connected():
            raise ConnectionError('mqtt_offline')
        info = self.client.publish(topic,json.dumps(body,allow_nan=False),qos=1,retain=retain)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            raise TimeoutError('publish_unconfirmed')

    def start(self):
        self.client.connect_async(self.cfg['MQTT_HOST'],self.cfg['MQTT_PORT'],60)
        self.client.loop_start()

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


def create_app(cfg, store=None, broker=None):
    app = Flask(__name__,template_folder=str(ROOT/"templates"),static_folder=None)
    app.config.update(SECRET_KEY=cfg['WEB_SECRET'],MAX_CONTENT_LENGTH=16384,
                      SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict')
    store = store if store is not None else Store(cfg)

    @app.before_request
    def allowed():
        # X-Forwarded-For는 사용하지 않는다. 허용 IP는 실제 연결 출발지로 검사한다.
        try:
            address = ipaddress.ip_address(request.remote_addr)
        except ValueError:
            abort(403)
        if not any(address in net for net in cfg['WEB_ALLOW_NETS']):
            abort(403)
        if request.method == 'POST':
            token = session.get('csrf')
            if not token or not secrets.compare_digest(token,request.headers.get('X-CSRF-Token','')):
                abort(403)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        return response

    def device():
        value = request.args.get('device','')
        if value not in cfg['DEVICES']:
            abort(404)
        return value

    @app.get('/')
    def index():
        session.setdefault('csrf',secrets.token_urlsafe(32))
        return render_template('field_web.html',site=cfg['SITE'],devices=sorted(cfg['DEVICES']),
                               csrf=session['csrf'],control=cfg['CONTROL'],door=cfg['CONTROL'])

    @app.get('/image/<name>')
    def image(name):
        if name not in ('door_open.png','door_closed.png'):
            abort(404)
        return send_from_directory(ROOT.parent/'image',name)

    @app.get('/api/state')
    def state():
        dev = device()
        try:
            saved = store.state(dev)
            commands = store.commands(dev)
            state = None
            if saved:
                state = {k:saved.get(k) for k in ('temp','target','door','cooling','stamp','eta','status')}
                if time.time()-saved['ts'] > cfg['STALE_SEC'] or saved['ts'] > time.time():
                    state.update(status='stale',eta=None)
            return jsonify(site=cfg['SITE'],device=dev,state=state,commands=commands,locked=store.locked(dev),
                           mqtt=broker.status() if broker else dict(connected=False,subscribed=False,last=None))
        except Exception as exc:
            log('field_db_failed', error=type(exc).__name__)
            return jsonify(error='시설 DB를 조회할 수 없습니다.'),503

    @app.get('/api/logs')
    def logs():
        dev = device()
        kind = request.args.get('kind','measurements')
        if kind not in ('measurements','alarms','commands'):
            abort(400)
        try:
            limit = int(request.args.get('limit','100'))
            if not 1 <= limit <= 500:
                abort(400)
        except ValueError:
            abort(400)
        try:
            return jsonify(kind=kind,rows=store.logs(dev,kind,limit))
        except Exception as exc:
            log('field_db_failed', error=type(exc).__name__)
            return jsonify(error='로그를 조회할 수 없습니다.'),503

    @app.post('/api/command')
    def command():
        dev = device()
        if not cfg['CONTROL'] or broker is None:
            return jsonify(error='제어 연결 설정이 완료되지 않았습니다.'),503
        data = request.get_json(silent=True)
        if not isinstance(data,dict):
            abort(400)
        action = data.get('action')
        body=dict(site_id=cfg['SITE'],device_id=dev,at=utc())
        retain=False
        if action=='door' and type(data.get('open')) is bool:
            body.update(action='open' if data['open'] else 'close',requested_by='web')
            channel=topic(cfg['SITE'],dev,'door')
        elif action=='lock' and type(data.get('locked')) is bool:
            body.update(locked=data['locked'],by='web')
            channel=topic(cfg['SITE'],dev,'lock')
            retain=True  # 이 토픽만 재접속한 controller에 최종 잠금 상태를 전달한다.
        else:
            return jsonify(error='문 조작 또는 현장 잠금만 요청할 수 있습니다.'),400
        if not broker.status()['connected']:
            return jsonify(error='현장 MQTT 연결이 끊겼습니다.'),503
        try:
            broker.publish(channel,body,retain=retain)
        except Exception as exc:
            log('field_publish_unconfirmed',error=type(exc).__name__)
            return jsonify(status='unconfirmed',error='전송 확인 실패 / 실제 상태를 확인해 주세요.'),504
        log('field_command_sent',site=cfg['SITE'],device=dev,action=action)
        return jsonify(status='submitted',message='전송 확인 / 상태 반영 대기'),202

    return app


def main():
    try:
        cfg = settings()
    except ValueError as exc:
        log('field_config_error',reason=str(exc))
        return 2
    broker = Broker(cfg) if cfg['MQTT'] else None
    app = create_app(cfg,broker=broker)
    try:
        if broker:
            broker.start()
        app.run(host=cfg['WEB_HOST'],port=cfg['WEB_PORT'],debug=False,use_reloader=False,threaded=True)
    finally:
        if broker:
            broker.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
