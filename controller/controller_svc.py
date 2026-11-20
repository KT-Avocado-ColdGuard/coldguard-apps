"""
원격 명령 검사기

- AWS cmd 검증 -> apply -> applied 확인 -> ack.
- MySQL은 SELECT/INSERT만 사용하며 재전송 작업은 STATE_DB에 보관한다.
- AWS에서 받은 온도 변경 명령을 검사합니다.
- 대상 장비 / 중복 / 발급&만료 시각 / 현장 잠금 / 허용 온도를 확인합니다.
- 검사를 통과한 명령만 시뮬레이터로 전달합니다.
- 실제 적용 응답을 확인하고 결과를 DB에 기록해 MQTT로 전달합니다.
- 실행: python controller_svc.py
"""
import json
import queue
import signal
import threading
import time
from datetime import datetime, timezone
from common.coldguard_protocol import (topic, split_topic, utc, parse_time, loads, number,
    command_reason, approved, lock_message, delivery, temperature, time_reason)
from common.coldguard_runtime import (Database, EventSender, journal, sql_time, settings as common,
    mqtt_client, publish, log)

MIN_TARGET,MAX_TARGET=-50.0,-10.0
is_number=number


def settings():
    return common('controller')


def shape(cmd):
    # DB NOT NULL/길이 조건을 만족할 수 없는 깨진 JSON은 콘솔 감사 로그로 거부한다.
    for key,limit in (('cmd_id',50),('site_id',20),('device_id',20),('action',20),('issued_by',50)):
        if not isinstance(cmd.get(key),str) or not 1<=len(cmd[key])<=limit:
            raise ValueError('invalid_'+key)
    parse_time(cmd.get('issued_at'))
    parse_time(cmd.get('expires_at'))
    if 'value' not in cmd:
        raise ValueError('missing_value')


class Store(Database):
    def decision(self,cmd,result,before,after):
        value=cmd['value']
        # 잘못된 값도 차단 이력은 남긴다. DECIMAL에 담을 수 없으면 NULL로 기록한다.
        if not number(value) or abs(value)>9999.99:
            value=None
        elif value is not None:
            value=temperature(value)
        args=(cmd['cmd_id'],cmd['site_id'],cmd['device_id'],cmd['action'],value,
              sql_time(cmd['issued_at']),sql_time(cmd['expires_at']),cmd['issued_by'],
              result['result'],result['reason'],before,after,sql_time(result['at']))
        db=self.connect()
        try:
            with db.cursor(dictionary=True) as cur:
                # 응답 유실 후 같은 기록을 재시도하는 경우만 건너뛴다.
                # 새 중복 시도는 별도 decided_at으로 INSERT한다(cmd_id UNIQUE 아님).
                cur.execute('SELECT id FROM command_log WHERE site_id=%s AND device_id=%s '
                            'AND cmd_id=%s AND decided_at=%s AND result=%s AND reason <=> %s LIMIT 1',
                            (cmd['site_id'],cmd['device_id'],cmd['cmd_id'],args[-1],result['result'],result['reason']))
                if not cur.fetchone():
                    cur.execute('INSERT INTO command_log(cmd_id,site_id,device_id,action,value,issued_at,'
                                'expires_at,issued_by,result,reason,setpoint_before,setpoint_after,decided_at) '
                                'VALUES('+','.join(['%s']*13)+')',args)
                if result['result']=='rejected':
                    self.insert_event(cur,dict(site_id=cmd['site_id'],device_id=cmd['device_id'],
                        event_type='cmd_rejected',severity='warning',temp_c=None,predicted_sec=None,
                        detail=cmd['cmd_id']+' / '+result['reason'],at=result['at']))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class Controller:
    def __init__(self,cfg,store,publish):
        self.cfg,self.store,self.publish=cfg,store,publish
        self.state=journal(cfg['STATE_DB'],'controller',cfg['SITE'])
        self.state.executescript('''
            CREATE TABLE IF NOT EXISTS commands(k TEXT PRIMARY KEY,body TEXT,packet TEXT,stage TEXT,
                started REAL,before_value REAL);
            CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,body TEXT,result TEXT,
                before_value REAL,after_value REAL,saved INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS locks(device TEXT PRIMARY KEY,locked INTEGER,at TEXT);
            CREATE TABLE IF NOT EXISTS lock_events(id INTEGER PRIMARY KEY,body TEXT);
            CREATE TABLE IF NOT EXISTS clock(id INTEGER PRIMARY KEY,last INTEGER);
            INSERT OR IGNORE INTO clock VALUES(1,0);
        ''')
        self.sender=EventSender(store,self.state,cfg['SITE'],('cmd_rejected','lock_on','lock_off'))
        self.count=None

    @staticmethod
    def key(cmd):
        return json.dumps([cmd['site_id'],cmd['device_id'],cmd['cmd_id']])

    def locked(self,device):
        row=self.state.execute('SELECT locked FROM locks WHERE device=?',(device,)).fetchone()
        return bool(row[0]) if row else False

    def lock(self,msg,source):
        locked=lock_message(msg,source)
        site,device,_=split_topic(source)
        if site!=self.cfg['SITE'] or device not in self.cfg['DEVICES']:
            raise ValueError('unknown_device')
        old=self.state.execute('SELECT locked,at FROM locks WHERE device=?',(device,)).fetchone()
        if old and parse_time(msg['at'])<=parse_time(old['at']):
            return
        with self.state:
            self.state.execute('INSERT OR REPLACE INTO locks VALUES(?,?,?)',(device,locked,msg['at']))
            if old is None or bool(old['locked'])!=locked:
                body=dict(site_id=site,device_id=device,event_type='lock_on' if locked else 'lock_off',
                          severity='info',temp_c=None,predicted_sec=None,detail='현장 잠금' if locked else '현장 잠금 해제',at=msg['at'])
                self.state.execute('INSERT INTO lock_events(body) VALUES(?)',(json.dumps(body,ensure_ascii=False),))

    def decision(self,cmd,reason,before=None,after=None):
        # 같은 밀리초의 중복 요청도 서로 다른 기록으로 남긴다.
        last=self.state.execute('SELECT last FROM clock WHERE id=1').fetchone()[0]
        stamp=max(time.time_ns()//1000000,last+1)
        self.state.execute('UPDATE clock SET last=? WHERE id=1',(stamp,))
        result=dict(cmd_id=cmd['cmd_id'],result='rejected' if reason else 'accepted',reason=reason,
                    at=utc(datetime.fromtimestamp(stamp/1000,timezone.utc)))
        self.state.execute('INSERT INTO decisions(body,result,before_value,after_value) VALUES(?,?,?,?)',
                           (json.dumps(cmd),json.dumps(result),before,after))
        if self.count:
            self.count['blocked' if reason else 'approved'].inc()
        return result

    def command(self,cmd,source=None):
        shape(cmd)
        source=source or topic(cmd['site_id'],cmd['device_id'],'cmd')
        src_site,src_device,_=split_topic(source)
        if src_site!=self.cfg['SITE']:
            raise ValueError('wrong_facility_topic')
        key=self.key(cmd)
        row=self.state.execute('SELECT * FROM commands WHERE k=?',(key,)).fetchone()
        # 영속 파일을 새로 만들었더라도 완료된 명령 ID는 DB 이력으로 확인한다.
        duplicate=bool(row) or bool(self.store.query('SELECT id FROM command_log WHERE site_id=%s '
            'AND device_id=%s AND cmd_id=%s LIMIT 1',(cmd['site_id'],cmd['device_id'],cmd['cmd_id'])))
        reason=command_reason(cmd,source,self.cfg['SITE'],self.cfg['DEVICES'],duplicate,
                              self.locked(src_device))
        if reason == 'expired':
            log('command_time_rejected',site_id=src_site,device_id=src_device,
                cmd_id=cmd['cmd_id'],reason='expired',detail=time_reason(cmd))
        # 수신 토픽은 응답 경로이자 DB 기록의 시설 범위다. 본문의 잘못된 시설로 발행하지 않는다.
        if reason=='unknown_device':
            cmd={**cmd,'site_id':src_site,'device_id':src_device}
            key=self.key(cmd)
            row=self.state.execute('SELECT * FROM commands WHERE k=?',(key,)).fetchone()
        latest=self.store.latest(cmd['site_id'],cmd['device_id']) if reason!='unknown_device' else None
        before=float(latest['setpoint_c']) if latest else None
        with self.state:
            if reason:
                self.decision(cmd,reason,before,None)
                # 중복 기록을 기존 승인 명령에 덮어쓰지 않는다.
                if not row:
                    self.state.execute('INSERT INTO commands VALUES(?,?,NULL,\'done\',NULL,?)',
                                       (key,json.dumps(cmd),before))
            else:
                packet=approved(cmd)
                self.state.execute('INSERT INTO commands VALUES(?,?,?,\'queued\',NULL,?)',
                                   (key,json.dumps(cmd),json.dumps(packet),before))

    def ack(self,msg,source=None):
        source=source or topic(msg.get('site_id'),msg.get('device_id'),'applied')
        site,device,channel=split_topic(source)
        if channel!='applied' or site!=self.cfg['SITE'] or device not in self.cfg['DEVICES']:
            raise ValueError('wrong_applied_topic')
        if (msg.get('site_id'),msg.get('device_id'))!=(site,device):
            raise ValueError('wrong_applied_identity')
        if not isinstance(msg.get('cmd_id'),str) or not number(msg.get('setpoint_c')):
            raise ValueError('invalid_applied')
        at=parse_time(msg.get('at'))
        row=self.state.execute('SELECT * FROM commands WHERE k=?',(self.key(msg),)).fetchone()
        if not row:
            raise ValueError('unexpected_applied')
        if row['stage']=='done':
            log('late_or_duplicate_applied',cmd_id=msg['cmd_id'])
            return
        cmd=json.loads(row['body'])
        if row['started'] is not None and time.time()-row['started']>=self.cfg['APPLY_TIMEOUT_SEC']:
            with self.state:
                self.decision(cmd,'apply_timeout',row['before_value'],None)
                self.state.execute("UPDATE commands SET stage='done' WHERE k=?",(self.key(cmd),))
            return
        if row['started'] is None or msg['setpoint_c']!=temperature(cmd['value']) or at>parse_time(cmd['expires_at']):
            raise ValueError('applied_mismatch')
        with self.state:
            self.decision(cmd,None,row['before_value'],msg['setpoint_c'])
            self.state.execute('UPDATE commands SET stage=\'done\' WHERE k=?',(self.key(cmd),))

    def flush(self):
        for row in self.state.execute("SELECT * FROM commands WHERE stage!='done'").fetchall():
            cmd=json.loads(row['body'])
            reason=None
            if row['started'] is not None and time.time()-row['started']>=self.cfg['APPLY_TIMEOUT_SEC']:
                reason='apply_timeout'
            elif time_reason(cmd):
                reason='expired' if row['started'] is None else 'apply_timeout'
            elif row['stage']=='queued' and self.locked(cmd['device_id']):
                reason='locked'
            if reason:
                with self.state:
                    self.decision(cmd,reason,row['before_value'],None)
                    self.state.execute("UPDATE commands SET stage='done' WHERE k=?",(row['k'],))
            elif row['stage']=='queued':
                if row['started'] is None:
                    with self.state:
                        self.state.execute('UPDATE commands SET started=? WHERE k=?',(time.time(),row['k']))
                self.publish(topic(cmd['site_id'],cmd['device_id'],'apply'),json.loads(row['packet']))
                with self.state:
                    self.state.execute("UPDATE commands SET stage='waiting' WHERE k=?",(row['k'],))
        for row in self.state.execute('SELECT * FROM decisions ORDER BY id').fetchall():
            cmd,result=json.loads(row['body']),json.loads(row['result'])
            if not row['saved']:
                self.store.decision(cmd,result,row['before_value'],row['after_value'])
                # DB 확정 뒤 콘솔에도 남겨 Fluent Bit/Loki에서 명령 ID로 검색한다.
                log('command_decision',site_id=cmd['site_id'],device_id=cmd['device_id'],
                    cmd_id=cmd['cmd_id'],result=result['result'],reason=result['reason'],
                    decided_at=result['at'])
                with self.state:
                    self.state.execute('UPDATE decisions SET saved=1 WHERE id=?',(row['id'],))
            # 토픽은 대상 장비 경로. 잘못된 대상도 이 경로의 ack에 차단 결과를 전달한다.
            self.publish(topic(cmd['site_id'],cmd['device_id'],'ack'),result)
            with self.state:
                self.state.execute('DELETE FROM decisions WHERE id=?',(row['id'],))
        for row in self.state.execute('SELECT * FROM lock_events ORDER BY id').fetchall():
            self.store.event(json.loads(row['body']))
            with self.state:
                self.state.execute('DELETE FROM lock_events WHERE id=?',(row['id'],))
        self.sender.flush(self.publish)


def main():
    try:
        cfg=settings()
    except ValueError as exc:
        log('controller_config_error',reason=str(exc))
        return 2
    from prometheus_client import CollectorRegistry,Counter,start_http_server
    registry=CollectorRegistry()
    client=mqtt_client(cfg)
    svc=Controller(cfg,Store(cfg),lambda t,b:publish(client,t,b))
    svc.count={name:Counter('coldguard_command_'+name+'_total','최종 명령 결정 건수',['site'],
                 registry=registry).labels(cfg['SITE']) for name in ('approved','blocked')}
    server,_=start_http_server(cfg['METRICS_PORT'],addr=cfg['METRICS_HOST'],registry=registry)
    inbox=queue.Queue(maxsize=1000)
    stop,overloaded=threading.Event(),threading.Event()
    def connected(c,u,f,reason,p):
        if not reason.is_failure:
            c.subscribe([(f"coldguard/{cfg['SITE']}/+/{ch}",1) for ch in ('lock','applied','cmd')])
        log('controller_mqtt_connect',reason=str(reason))
    def message(c,u,msg):
        try:
            inbox.put_nowait(msg)
        except queue.Full:
            overloaded.set()
    client.on_connect,client.on_message=connected,message
    client.on_connect_fail=lambda c,u:log('controller_mqtt_connect_failed',site_id=cfg['SITE'])
    from common.coldguard_runtime import watch_subscriptions
    watch_subscriptions(client,cfg,'controller',3,overloaded.set)
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *args:stop.set())
    client.connect_async(cfg['MQTT_HOST'],cfg['MQTT_PORT'],60)
    client.loop_start()
    pending=None
    try:
        while not stop.is_set() and not overloaded.is_set():
            try:
                if pending is None:
                    try: pending=inbox.get(timeout=.1)
                    except queue.Empty: pass
                if pending is not None:
                    try:
                        site,device,channel=split_topic(pending.topic)
                        delivery(channel,pending.qos,pending.retain)
                        body=loads(pending.payload)
                        if channel=='lock': svc.lock(body,pending.topic)
                        elif channel=='cmd': svc.command(body,pending.topic)
                        elif channel=='applied': svc.ack(body,pending.topic)
                        else: raise ValueError('unexpected_channel')
                    except (ValueError,UnicodeError,TypeError) as exc:
                        log('controller_message_rejected',reason=str(exc))
                    client.ack(pending.mid,pending.qos)
                    pending=None
                # 오프라인 중에도 만료 판단은 필요하지만 네트워크 재전송은 실패 후 재시도한다.
                svc.flush()
            except Exception as exc:
                log('controller_retry',error=type(exc).__name__)
                stop.wait(1)
        return 1 if overloaded.is_set() else 0
    finally:
        client.disconnect()
        client.loop_stop()
        svc.state.close()
        server.shutdown()
        server.server_close()


if __name__=='__main__':
    raise SystemExit(main())
