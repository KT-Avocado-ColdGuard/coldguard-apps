"""
공통 실행 기능

- 환경변수를 우선 사용하고 없으면 coldguard_config.py 기본값을 읽습니다.
- MQTT TLS 연결 / 메시지 발행 / 구독 승인 확인을 지원합니다.
- MySQL 접근 / 로컬 상태 저장 / 이벤트 재전송 / JSON 로그 기능을 제공합니다.
- 다른 프로그램에서 가져다 쓰므로 직접 실행하지 않습니다.
"""
import json
import math
import os
from common.coldguard_config import values as config_values
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from common.coldguard_protocol import identity, topic, utc, parse_time


def log(event, **fields):
    print(json.dumps(dict(event=event, at=utc(), **fields), ensure_ascii=False, allow_nan=False), flush=True)


def settings(role, env=None):
    env = config_values(role) if env is None else env
    defaults = dict(SITE='site01', MQTT_HOST='mosquitto', MQTT_PORT='8883',
                    MQTT_CA='/etc/coldguard/ca.crt', MYSQL_HOST='mysql', MYSQL_PORT='3306',
                    MYSQL_DB='coldguard', METRICS_HOST='0.0.0.0',
                    METRICS_PORT='9201' if role == 'collector' else '9202')
    keys = ('SITE','DEVICES','MQTT_HOST','MQTT_PORT','MQTT_CA','MQTT_USER','MQTT_PASS',
            'MYSQL_HOST','MYSQL_PORT','MYSQL_DB','MYSQL_USER','MYSQL_PASSWORD',
            'METRICS_HOST','METRICS_PORT')
    cfg = {k:env.get(k,defaults.get(k,'')) for k in keys}
    missing = [k for k,v in cfg.items() if not v]
    if missing:
        raise ValueError('설정 누락 : '+', '.join(missing))
    cfg['DEVICES'] = {d.strip() for d in cfg['DEVICES'].split(',') if d.strip()}
    if not cfg['DEVICES']:
        raise ValueError('DEVICES 누락')
    for d in cfg['DEVICES']:
        identity(cfg['SITE'], d)
    for k in ('MQTT_PORT','MYSQL_PORT','METRICS_PORT'):
        cfg[k] = int(cfg[k])
        if not 1 <= cfg[k] <= 65535:
            raise ValueError(k+' 범위 오류')
    cfg['MQTT_CLIENT_ID'] = env.get('MQTT_CLIENT_ID', role+'-'+cfg['SITE'])
    cfg['STATE_DB'] = env.get('STATE_DB','')
    if role != 'web' and not cfg['STATE_DB']:
        raise ValueError('STATE_DB에 서비스별 영속 파일 경로를 지정하세요.')
    if role in ('collector','web'):
        cfg['STALE_SEC'] = float(env.get('STALE_SEC','0'))
        if not math.isfinite(cfg['STALE_SEC']) or cfg['STALE_SEC'] <= 0:
            raise ValueError('STALE_SEC에 합의한 데이터 단절 기준을 지정하세요.')
    cfg['APPLY_TIMEOUT_SEC'] = 5.0
    return cfg


def sql_time(value):
    # DATETIME(3)에 맞춰 밀리초 단위 UTC로 고정한다.
    dt = parse_time(value) if isinstance(value,str) else value
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt
    return dt.replace(tzinfo=None, microsecond=(dt.microsecond//1000)*1000)


def wire_time(value):
    return utc(value.replace(tzinfo=timezone.utc))


def journal(path, role, site):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS identity(role TEXT,site TEXT)')
    old = db.execute('SELECT * FROM identity').fetchone()
    if old and (old['role'],old['site']) != (role,site):
        db.close()
        raise ValueError('STATE_DB는 서비스/시설별로 분리해야 합니다.')
    if not old:
        db.execute('INSERT INTO identity VALUES(?,?)',(role,site))
    db.commit()
    return db


class Database:
    def __init__(self,cfg):
        self.cfg = cfg

    def connect(self):
        import mysql.connector
        c = self.cfg
        return mysql.connector.connect(host=c['MYSQL_HOST'],port=c['MYSQL_PORT'],
            database=c['MYSQL_DB'],user=c['MYSQL_USER'],password=c['MYSQL_PASSWORD'],
            time_zone='+00:00',connection_timeout=5,autocommit=False)

    def query(self,sql,args=()):
        db=self.connect()
        try:
            with db.cursor(dictionary=True) as cur:
                cur.execute(sql,args)
                return cur.fetchall()
        finally:
            db.close()

    def latest(self,site,device):
        rows=self.query('SELECT * FROM telemetry WHERE site_id=%s AND device_id=%s '
                        'ORDER BY measured_at DESC,seq DESC LIMIT 1',(site,device))
        return rows[0] if rows else None

    @staticmethod
    def insert_event(cur,event):
        keys=('site_id','device_id','event_type','severity','temp_c','predicted_sec','detail')
        args=tuple(event.get(k) for k in keys)+(sql_time(event['at']),)
        # MySQL에는 수정 권한이 없다. 동일 작업의 재시도만 조회 후 건너뛴다.
        cur.execute('SELECT id FROM event_log WHERE site_id=%s AND device_id=%s '
                    'AND event_type=%s AND occurred_at=%s AND detail <=> %s LIMIT 1',
                    (event['site_id'],event['device_id'],event['event_type'],args[-1],event.get('detail')))
        if not cur.fetchone():
            cur.execute('INSERT INTO event_log(site_id,device_id,event_type,severity,temp_c,'
                        'predicted_sec,detail,occurred_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',args)

    def event(self,event):
        db=self.connect()
        try:
            with db.cursor(dictionary=True) as cur:
                self.insert_event(cur,event)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class EventSender:
    # DB에 기록된 자기 서비스 이벤트를 순서대로 발행. PUBACK 이후에만 커서를 저장.
    def __init__(self,db,state,site,types):
        self.db,self.state,self.site,self.types=db,state,site,types
        state.execute('CREATE TABLE IF NOT EXISTS event_cursor(id INTEGER PRIMARY KEY,last INTEGER)')
        state.execute('INSERT OR IGNORE INTO event_cursor VALUES(1,0)')
        state.commit()

    def flush(self,publish):
        last=self.state.execute('SELECT last FROM event_cursor WHERE id=1').fetchone()[0]
        placeholders=','.join(['%s']*len(self.types))
        rows=self.db.query('SELECT * FROM event_log WHERE site_id=%s AND id>%s '
                           f'AND event_type IN ({placeholders}) ORDER BY id LIMIT 100',
                           (self.site,last,*self.types))
        for r in rows:
            body={k:r[k] for k in ('site_id','device_id','event_type','severity','temp_c','predicted_sec','detail')}
            body['temp_c']=float(body['temp_c']) if body['temp_c'] is not None else None
            body['at']=wire_time(r['occurred_at'])
            publish(topic(r['site_id'],r['device_id'],'event'),body)
            with self.state:
                self.state.execute('UPDATE event_cursor SET last=? WHERE id=1',(r['id'],))


def mqtt_client(cfg):
    import paho.mqtt.client as mqtt
    client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id=cfg['MQTT_CLIENT_ID'],
                       clean_session=False,manual_ack=True)
    client.username_pw_set(cfg['MQTT_USER'],cfg['MQTT_PASS'])
    client.tls_set(ca_certs=cfg['MQTT_CA'])
    client.connect_timeout=5
    client.reconnect_delay_set(1,30)
    return client


def publish(client,channel,body,retain=False):
    info=client.publish(channel,json.dumps(body,ensure_ascii=False,allow_nan=False),qos=1,retain=retain)
    info.wait_for_publish(timeout=5)
    if not info.is_published():
        raise ConnectionError('publish_unconfirmed')


def watch_subscriptions(client, cfg, role, expected, on_failure=None):
    # SUBACK 성공과 필요한 QoS 1 승인 여부를 별도로 기록한다.
    def subscribed(c,u,mid,reasons,properties):
        ok=len(reasons)==expected and all(not r.is_failure and r.value==1 for r in reasons)
        log('mqtt_subscription_result',service=role,site_id=cfg['SITE'],mid=mid,
            success=ok,codes=[r.value for r in reasons])
        if not ok and on_failure:
            on_failure()
    client.on_subscribe=subscribed
    previous=client.on_disconnect
    def disconnected(c,u,flags,reason,properties):
        log('mqtt_disconnected',service=role,site_id=cfg['SITE'],reason=str(reason))
        if previous:
            previous(c,u,flags,reason,properties)
    client.on_disconnect=disconnected
