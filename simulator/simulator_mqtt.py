"""
시뮬레이터의 선택적 MQTT 연결. SQLite에 미전송 자료/명령 결과를 보관한다.

시뮬레이터 통신
- 시뮬레이터가 만든 측정값을 MQTT로 전송합니다.
- 승인된 온도 변경 명령과 현장 웹의 문 조작 요청을 받습니다.
- 온도 변경 후 실제 적용 결과를 응답합니다.
- 순번과 미전송 메시지를 파일에 저장해 재시작/재연결에 대비합니다.
- 직접 실행하지 않고 freezer_simulator.py가 사용합니다.
"""
import hashlib
import json
import os
from common.coldguard_config import values as config_values
import queue
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone

from common.coldguard_protocol import identity, topic, utc, check_apply, door, loads


def settings(env=None):
    env = config_values('simulator') if env is None else env
    if env.get("SIM_MQTT", "0") != "1":
        return None
    defaults = dict(SITE="site01", DEVICE="fz01", MQTT_PORT="8883")
    names = ("SITE", "DEVICE", "MQTT_HOST", "MQTT_PORT", "MQTT_USER",
             "MQTT_PASS", "MQTT_CA", "SIM_DB")
    cfg = {k: env.get(k, defaults.get(k, "")).strip() for k in names}
    cfg["MQTT_PASS"] = env.get("MQTT_PASS", "")
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise ValueError("설정 누락 : " + ", ".join(missing))
    identity(cfg["SITE"], cfg["DEVICE"])
    if cfg["MQTT_USER"] != cfg["DEVICE"]:
        raise ValueError("시뮬레이터 MQTT_USER는 DEVICE와 같아야 합니다.")
    cfg["MQTT_CLIENT_ID"] = env.get("MQTT_CLIENT_ID", f"sim-{cfg['SITE']}-{cfg['DEVICE']}")
    cfg["MQTT_PORT"] = int(cfg["MQTT_PORT"])
    if not 1 <= cfg["MQTT_PORT"] <= 65535:
        raise ValueError("MQTT_PORT 범위 오류")
    for channel in ("telemetry", "apply", "applied", "door"):
        cfg[channel.upper()+"_TOPIC"] = topic(cfg["SITE"], cfg["DEVICE"], channel)

    return cfg


def all_settings(env=None):
    # 한 프로세스에는 하나의 시설/장비 연결만 만든다.
    env = config_values('simulator') if env is None else env
    if env.get("SIM_MQTT", "0") == "1" and any(k in env for k in ("SIM_SITES", "A_SITE", "B_SITE")):
        raise ValueError("이전 A/B 설정 대신 SITE/DEVICE를 사용하세요.")
    cfg = settings(env)
    return [cfg] if cfg else []


class Link:
    def __init__(self, cfg, freezer):
        self.cfg, self.freezer = cfg, freezer
        self.guard = threading.RLock()
        Path(cfg['SIM_DB']).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cfg["SIM_DB"], check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS counter(id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL);
            INSERT OR IGNORE INTO counter VALUES(1,0);
            CREATE TABLE IF NOT EXISTS door_state(id INTEGER PRIMARY KEY CHECK(id=1), at TEXT, opened INTEGER);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, topic TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, digest TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), site TEXT, device TEXT, target REAL);
        ''')
        row = self.db.execute("SELECT site,device,target FROM state WHERE id=1").fetchone()
        if row and row[:2] != (cfg["SITE"], cfg["DEVICE"]):
            self.db.close()
            raise ValueError("SIM_DB는 시설/장비마다 별도 파일이어야 합니다.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO state VALUES(1,?,?,?)",
                            (cfg["SITE"], cfg["DEVICE"], freezer.target))
        if row:
            freezer.target = row[2]
        self.stop = threading.Event()
        self.inbox = queue.Queue(maxsize=1000)
        self.client = None
        self.thread = None

    def enqueue(self, topic, data):
        self.db.execute("INSERT INTO outbox(topic,body) VALUES(?,?)",
                        (topic, json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def sample(self):
        snap = self.freezer.snapshot()
        if snap["stamp"] is None:
            return
        with self.guard, self.db:
            seq = self.db.execute("SELECT seq FROM counter WHERE id=1").fetchone()[0] + 1
            stamp = datetime.fromisoformat(snap["stamp"].replace("Z", "+00:00"))
            data = dict(site_id=self.cfg["SITE"], device_id=self.cfg["DEVICE"], seq=seq,
                        measured_at=utc(stamp), temp_c=round(snap["temp"], 1),
                        setpoint_c=snap["target"], door_open=snap["door"], cooling=snap["cooling"],
                        status="high_temp" if round(snap["temp"], 1) > -25 else "normal")
            # 순번 증가와 전송 대기 저장을 함께 확정한다. 재전송에는 새 순번을 만들지 않는다.
            self.db.execute("UPDATE counter SET seq=? WHERE id=1", (seq,))
            self.enqueue(self.cfg["TELEMETRY_TOPIC"], data)

    def command(self, cmd):
        if (cmd.get("site_id"), cmd.get("device_id")) != (self.cfg["SITE"], self.cfg["DEVICE"]):
            raise ValueError("wrong_target")
        if not isinstance(cmd.get("cmd_id"), str) or not 1 <= len(cmd["cmd_id"]) <= 50:
            raise ValueError("invalid_cmd_id")
        digest = hashlib.sha256(json.dumps(cmd, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.guard, self.freezer.lock:
            old = self.db.execute("SELECT digest,body FROM commands WHERE id=?", (cmd["cmd_id"],)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError("duplicate_id_conflict")
                # 이미 적용한 명령은 만료 후에도 기존 결과만 재전송하고 재적용하지 않는다.
                result = json.loads(old[1])
                with self.db:
                    self.enqueue(self.cfg["APPLIED_TOPIC"], result)
                return result
            target = check_apply(cmd, self.cfg["APPLY_TOPIC"])
            result = dict(site_id=self.cfg["SITE"], device_id=self.cfg["DEVICE"],
                          cmd_id=cmd["cmd_id"], setpoint_c=target, at=utc())
            with self.db:
                self.db.execute("INSERT INTO commands VALUES(?,?,?)",
                                (cmd["cmd_id"], digest, json.dumps(result)))
                self.db.execute("UPDATE state SET target=? WHERE id=1", (target,))
                self.enqueue(self.cfg["APPLIED_TOPIC"], result)
            self.freezer.target = target
            self.freezer.swing_time = 0
            self.freezer.update_alarm()
            return result

    def set_door(self, data):
        opened = door(data, self.cfg["DOOR_TOPIC"])
        from common.coldguard_protocol import parse_time
        with self.guard:
            old = self.db.execute("SELECT at FROM door_state WHERE id=1").fetchone()
            if old and parse_time(data["at"]) <= parse_time(old[0]):
                return  # 지연 재전송된 문 조작이 최신 조작을 되돌리지 않는다.
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO door_state VALUES(1,?,?)", (data["at"], opened))
            self.freezer.set_door(opened)

    def flush_one(self):
        with self.guard:
            row = self.db.execute("SELECT id,topic,body FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return
        info = self.client.publish(row[1], row[2], qos=1, retain=False)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            raise RuntimeError("publish_unconfirmed")
        with self.guard, self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (row[0],))

    def start(self):
        import paho.mqtt.client as mqtt
        cfg = self.cfg
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["MQTT_CLIENT_ID"],
                                  clean_session=False, manual_ack=True)
        self.client.username_pw_set(cfg["MQTT_USER"], cfg["MQTT_PASS"])
        self.client.tls_set(ca_certs=cfg["MQTT_CA"])
        self.client.connect_timeout = 5
        self.client.reconnect_delay_set(1, 30)
        def connected(client, userdata, flags, reason, properties):
            print(json.dumps(dict(event="sim_mqtt_connect", site=cfg["SITE"],
                                  reason=str(reason))), flush=True)
            if not reason.is_failure:
                client.subscribe([(cfg["APPLY_TOPIC"], 1), (cfg["DOOR_TOPIC"], 1)])
        def message(client, userdata, msg):
            try:
                self.inbox.put_nowait(msg)
            except queue.Full:
                # 수신 확인을 하지 않는다. 재시작 시 브로커가 다시 전달한다.
                self.stop.set()
                print('{"event":"sim_inbox_full_restart_required"}', flush=True)
        def failed(client, userdata):
            print(json.dumps(dict(event="sim_mqtt_connect_failed", site=cfg["SITE"])), flush=True)
        self.client.on_connect_fail = failed
        self.client.on_connect, self.client.on_message = connected, message
        from common.coldguard_runtime import watch_subscriptions
        watch_subscriptions(self.client,cfg,'simulator',2,self.stop.set)
        self.client.connect_async(cfg["MQTT_HOST"], cfg["MQTT_PORT"], keepalive=60)
        self.client.loop_start()
        self.thread = threading.Thread(target=self.run, name="sim-mqtt", daemon=True)
        self.thread.start()

    def run(self):
        pending = None
        while not self.stop.wait(0.05):
            try:
                if pending is None:
                    try:
                        pending = self.inbox.get_nowait()
                    except queue.Empty:
                        pass
                if pending is not None:
                    try:
                        if pending.qos != 1 or pending.retain or pending.topic not in (self.cfg["APPLY_TOPIC"], self.cfg["DOOR_TOPIC"]):
                            raise ValueError("invalid_delivery")
                        data = loads(pending.payload)
                        if pending.topic == self.cfg["APPLY_TOPIC"]:
                            self.command(data)
                        else:
                            self.set_door(data)
                    except (ValueError, UnicodeError) as exc:
                        print(json.dumps(dict(event="sim_command_rejected", error=type(exc).__name__)), flush=True)
                    self.client.ack(pending.mid, pending.qos)
                    pending = None
                if self.client.is_connected():
                    self.flush_one()
            except Exception as exc:
                print(json.dumps(dict(event="sim_mqtt_retry", error=type(exc).__name__)), flush=True)
                self.stop.wait(2)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=8)
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
        self.db.close()
