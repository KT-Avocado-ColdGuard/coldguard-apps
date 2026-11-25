"""중앙 수집 워커 검사/저장 로직 시험. MQTT·MySQL에 실제로 연결하지 않는다."""
import itertools
import json
import signal
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from decimal import Decimal

from central_worker import central_worker as cw

TEL_TOPIC = 'coldguard/site01/fz01/telemetry'
EVT_TOPIC = 'coldguard/site01/fz01/event'
ACK_TOPIC = 'coldguard/site01/fz01/ack'
NOW = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc).timestamp()


def tel(**over):
    body = {'site_id': 'site01', 'device_id': 'fz01', 'seq': 356095,
            'measured_at': '2026-11-24T08:22:30.000000Z', 'temp_c': -29.9, 'setpoint_c': -30.0,
            'door_open': False, 'cooling': True, 'status': 'normal'}
    body.update(over)
    return body


def evt(**over):
    body = {'site_id': 'site01', 'device_id': 'fz01', 'event_type': 'early_warning',
            'severity': 'warning', 'temp_c': -29.7, 'predicted_sec': 118,
            'detail': '고온 예측 경보', 'at': '2026-11-24T07:02:04Z'}
    body.update(over)
    return body


def ack(**over):
    body = {'cmd_id': 'central-test-001', 'result': 'accepted', 'reason': None,
            'at': '2026-11-24T08:49:35.653000Z'}
    body.update(over)
    return body


class DupError(Exception):
    errno = cw.DUPLICATE_KEY


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args):
        self.conn.calls.append(sql.split()[0])
        if sql.startswith('INSERT'):
            if self.conn.fail:
                raise self.conn.fail
            if self.conn.existing is not None:
                raise DupError()

    def fetchone(self):
        return self.conn.existing

    def close(self):
        pass


class FakeConn:
    def __init__(self, existing=None, fail=None):
        self.existing, self.fail, self.calls = existing, fail, []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.calls.append('COMMIT')

    def rollback(self):
        self.calls.append('ROLLBACK')

    def is_connected(self):
        return not self.closed

    def close(self):
        self.closed = True


class FakeStore(cw.Store):
    def __init__(self, conn):
        super().__init__({})
        self.conn = conn

    def connect(self):
        return self.conn


class SettingsTest(unittest.TestCase):
    def test_defaults_need_passwords(self):
        with self.assertRaises(ValueError):
            cw.settings({})

    def test_defaults(self):
        cfg = cw.settings({'MQTT_PASS': 'x', 'MYSQL_PASSWORD': 'y'})
        self.assertEqual(cfg['MQTT_PORT'], 1883)
        self.assertEqual(cfg['MYSQL_DB'], 'coldguard_central')
        self.assertEqual(cfg['TOPICS'], ['$share/central-worker/coldguard/+/+/telemetry',
                                         '$share/central-worker/coldguard/+/+/event',
                                         '$share/central-worker/coldguard/+/+/ack'])


    def test_pod_name_decides_drain(self):
        # HPA 최소 2개: 0, 1번은 구독 유지, 2번부터는 종료 전에 구독 해제 후 비우기
        for client_id, expected in (('central-worker-0', False), ('central-worker-1', False),
                                    ('central-worker-2', True), ('central-worker-3', True),
                                    ('central-worker', False)):
            with self.subTest(client_id=client_id):
                cfg = cw.settings({'MQTT_PASS': 'x', 'MYSQL_PASSWORD': 'y', 'MQTT_CLIENT_ID': client_id})
                self.assertEqual(cfg['DRAIN_ON_STOP'], expected)

    def test_stable_replicas_follows_setting(self):
        cfg = cw.settings({'MQTT_PASS': 'x', 'MYSQL_PASSWORD': 'y',
                           'MQTT_CLIENT_ID': 'central-worker-2', 'STABLE_REPLICAS': '3'})
        self.assertFalse(cfg['DRAIN_ON_STOP'])
        with self.assertRaises(ValueError):
            cw.settings({'MQTT_PASS': 'x', 'MYSQL_PASSWORD': 'y', 'STABLE_REPLICAS': '0'})


class DecodeTest(unittest.TestCase):
    def test_telemetry(self):
        row, stamp = cw.decode_telemetry(tel(), TEL_TOPIC)
        self.assertEqual(row[:3], ('site01', 'fz01', 356095))
        self.assertEqual(row[4], Decimal('-29.9'))
        self.assertEqual(row[3], datetime(2026, 11, 24, 8, 22, 30))

    def test_telemetry_topic_mismatch(self):
        with self.assertRaises(ValueError):
            cw.decode_telemetry(tel(device_id='fz02'), TEL_TOPIC)

    def test_telemetry_precision(self):
        with self.assertRaises(ValueError):
            cw.decode_telemetry(tel(temp_c=-29.951), TEL_TOPIC)

    def test_event(self):
        row, _ = cw.decode_event(evt(), EVT_TOPIC)
        self.assertEqual(row[2:4], ('early_warning', 'warning'))
        self.assertEqual(row[7], datetime(2026, 11, 24, 7, 2, 4))

    def test_event_nullable(self):
        row, _ = cw.decode_event(evt(event_type='lock_on', severity='info', temp_c=None,
                                     predicted_sec=None, detail='현장 잠금'), EVT_TOPIC)
        self.assertIsNone(row[4])

    def test_event_invalid(self):
        for bad in (dict(event_type='unknown'), dict(severity='high'), dict(predicted_sec=-1),
                    dict(predicted_sec=True), dict(detail='x' * 256), dict(site_id='site02'),
                    dict(at='2026-11-24T07:02:04')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cw.decode_event(evt(**bad), EVT_TOPIC)

    def test_ack(self):
        row, _ = cw.decode_ack(ack(), ACK_TOPIC)
        self.assertEqual(row[:5], ('central-test-001', 'site01', 'fz01', 'accepted', None))
        self.assertEqual(row[5], datetime(2026, 11, 24, 8, 49, 35, 653000))

    def test_ack_rejected(self):
        row, _ = cw.decode_ack(ack(result='rejected', reason='expired'), ACK_TOPIC)
        self.assertEqual(row[3:5], ('rejected', 'expired'))

    def test_ack_invalid(self):
        for bad in (dict(result='done'), dict(result='accepted', reason='locked'),
                    dict(result='rejected', reason=None), dict(cmd_id=''), dict(cmd_id='x' * 51)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cw.decode_ack(ack(**bad), ACK_TOPIC)


class HandleTest(unittest.TestCase):
    def run_handle(self, topic, body, store=None, qos=1, retain=False):
        store = store or FakeStore(FakeConn())
        return cw.handle(store, topic, json.dumps(body).encode(), qos, retain, NOW, 300)

    def test_stored(self):
        self.assertEqual(self.run_handle(TEL_TOPIC, tel())[2], 'stored')

    def test_delivery_rules(self):
        for qos, retain in ((0, False), (1, True)):
            with self.subTest(qos=qos, retain=retain), self.assertRaises(ValueError):
                self.run_handle(TEL_TOPIC, tel(), qos=qos, retain=retain)

    def test_future_stamp(self):
        with self.assertRaises(ValueError):
            self.run_handle(TEL_TOPIC, tel(measured_at='2026-11-24T09:10:00.000000Z'))

    def test_wrong_value_types_are_rejected_not_retried(self):
        # 목록처럼 엉뚱한 형식이 와도 ValueError(거부)로 끝나야 무한 재시도가 없다.
        for topic, body in ((EVT_TOPIC, evt(event_type=['x'])), (ACK_TOPIC, ack(reason=['x'], result='rejected')),
                            (TEL_TOPIC, '[' * 5000 + ']' * 5000)):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                cw.handle(FakeStore(FakeConn()), topic, payload, 1, False, NOW, 300)

    def test_bad_topic(self):
        with self.assertRaises(ValueError):
            self.run_handle('coldguard/site01/fz01/cmd', tel())


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.row, _ = cw.decode_telemetry(tel(), TEL_TOPIC)
        # DB가 돌려주는 형식: BOOLEAN은 0/1, DECIMAL은 Decimal
        self.db_row = tuple(int(v) if isinstance(v, bool) else v for v in self.row)

    def test_duplicate_same(self):
        conn = FakeConn(existing=self.db_row)
        self.assertEqual(FakeStore(conn).save('telemetry', self.row), 'duplicate')

    def test_duplicate_conflict(self):
        changed = self.db_row[:4] + (Decimal('-25.00'),) + self.db_row[5:]
        with self.assertRaises(ValueError):
            FakeStore(FakeConn(existing=changed)).save('telemetry', self.row)

    def test_db_error_reconnects(self):
        conn = FakeConn(fail=ConnectionError('down'))
        store = FakeStore(conn)
        with self.assertRaises(ConnectionError):
            store.save('telemetry', self.row)
        self.assertIsNone(store.db)
        self.assertTrue(conn.closed)


class ShutdownTest(unittest.TestCase):
    def test_db_error_then_sigterm_enters_drain(self):
        # 늘어난 파드(2번)가 DB 오류로 재시도하는 중에 종료 신호를 받아도 구독 해제·남은 처리를 거쳐야 한다.
        cfg = cw.settings({'MQTT_PASS': 'test', 'MYSQL_PASSWORD': 'test',
                           'MQTT_CLIENT_ID': 'central-worker-2'})
        handlers = {}
        client = Mock()
        message = SimpleNamespace(topic=TEL_TOPIC, payload=b'{}', qos=1, retain=False, mid=10)
        # 실제 MQTT 연결 없이 메시지 하나를 받은 상황을 만든다.
        client.loop_start.side_effect = lambda: client.on_message(client, None, message)
        attempts = 0

        def save_attempt(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # 첫 저장 실패와 동시에 종료 신호를 받는다.
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                raise ConnectionError('temporary DB failure')
            return 'telemetry', (), 'stored'  # 종료 처리 진입 후 재시도는 성공

        clock = itertools.count(1000)
        with (
            patch.object(cw, 'settings', return_value=cfg),
            patch.object(cw, 'mqtt_client', return_value=client),
            patch.object(cw, 'Store'),
            patch.object(cw, 'Metrics'),
            patch.object(cw.signal, 'signal',
                         side_effect=lambda sig, handler: handlers.__setitem__(sig, handler)),
            patch.object(cw, 'handle', side_effect=save_attempt),
            patch.object(cw, 'now', side_effect=lambda: next(clock)),
        ):
            result = cw.main()

        client.unsubscribe.assert_called_once_with(cfg['TOPICS'])
        client.ack.assert_called_once_with(10, 1)
        self.assertEqual(attempts, 2)
        self.assertEqual(result, 0)


class RecoverTest(unittest.TestCase):
    """회수 모드(DRAIN_ONLY)와 비우기 마감 처리."""

    def run_main(self, env, client, handle_effect, sleep=None):
        cfg = cw.settings({'MQTT_PASS': 'test', 'MYSQL_PASSWORD': 'test', **env})
        clock = itertools.count(1000)
        with (
            patch.object(cw, 'settings', return_value=cfg),
            patch.object(cw, 'mqtt_client', return_value=client),
            patch.object(cw, 'Store'),
            patch.object(cw, 'Metrics'),
            patch.object(cw.signal, 'signal'),
            patch.object(cw, 'handle', side_effect=handle_effect),
            patch.object(cw, 'now', side_effect=lambda: next(clock)),
            patch.object(cw.time, 'sleep', side_effect=sleep or (lambda s: None)),
        ):
            return cfg, cw.main()

    @staticmethod
    def message(mid):
        return SimpleNamespace(topic=TEL_TOPIC, payload=b'{}', qos=1, retain=False, mid=mid)

    def test_settings_require_explicit_client_id(self):
        base = {'MQTT_PASS': 'x', 'MYSQL_PASSWORD': 'y', 'DRAIN_ONLY': 'true'}
        with self.assertRaises(ValueError):
            cw.settings(base)
        with self.assertRaises(ValueError):
            cw.settings({**base, 'MQTT_CLIENT_ID': 'central-worker-3', 'DRAIN_ONLY': 'yes'})
        cfg = cw.settings({**base, 'MQTT_CLIENT_ID': 'central-worker-3'})
        self.assertTrue(cfg['DRAIN_ONLY'])

    def test_recover_drains_session_without_subscribing(self):
        client = Mock()
        ok = SimpleNamespace(is_failure=False)

        def start():
            # 접속하면 브로커가 세션에 보관하던 메시지 2개를 보낸다.
            client.on_connect(client, None, None, ok, None)
            client.on_message(client, None, self.message(1))
            client.on_message(client, None, self.message(2))
        client.loop_start.side_effect = start
        cfg, result = self.run_main({'MQTT_CLIENT_ID': 'central-worker-3', 'DRAIN_ONLY': 'true'},
                                    client, lambda *a: ('telemetry', (), 'stored'))
        client.subscribe.assert_not_called()
        client.unsubscribe.assert_called_once_with(cfg['TOPICS'])
        self.assertEqual([c.args for c in client.ack.call_args_list], [(1, 1), (2, 1)])
        self.assertEqual(result, 0)

    def test_recover_reports_failure_when_db_stays_down(self):
        client = Mock()
        ok = SimpleNamespace(is_failure=False)

        def start():
            client.on_connect(client, None, None, ok, None)
            client.on_message(client, None, self.message(1))
        client.loop_start.side_effect = start

        def db_down(*a):
            raise ConnectionError('db down')
        _, result = self.run_main({'MQTT_CLIENT_ID': 'central-worker-3', 'DRAIN_ONLY': 'true',
                                   'DRAIN_SEC': '20'}, client, db_down)
        client.ack.assert_not_called()  # 저장 못 한 메시지는 확인하지 않아 세션에 남는다
        self.assertEqual(result, 1)

    def test_drain_deadline_with_messages_still_flowing_is_failure(self):
        # 마감에 닿았는데 메시지가 계속 오면, 잠깐 비는 순간이 있어도 성공(0)으로 보고하지 않는다.
        # 실제 시간으로 0.05초마다 메시지가 오게 하고 마감은 1초로 둔다.
        client = Mock()
        signal_calls = []
        flowing = threading.Event()
        ok = SimpleNamespace(is_failure=False)

        def feed():
            mid = 1
            while not flowing.is_set():
                client.on_message(client, None, self.message(mid))
                mid += 1
                time.sleep(0.05)

        def start():
            client.on_connect(client, None, None, ok, None)
            threading.Thread(target=feed, daemon=True).start()
        client.loop_start.side_effect = start

        sent = []

        def store_and_stop(*a):
            if not sent:  # 첫 저장 때 종료 신호를 보낸다
                sent.append(True)
                [h for sig, h in signal_calls if sig == signal.SIGTERM][0](signal.SIGTERM, None)
            return 'telemetry', (), 'stored'

        cfg = cw.settings({'MQTT_PASS': 't', 'MYSQL_PASSWORD': 't',
                           'MQTT_CLIENT_ID': 'central-worker-2', 'DRAIN_SEC': '1'})
        try:
            with (
                patch.object(cw, 'settings', return_value=cfg),
                patch.object(cw, 'mqtt_client', return_value=client),
                patch.object(cw, 'Store'),
                patch.object(cw, 'Metrics'),
                patch.object(cw.signal, 'signal', side_effect=lambda *a: signal_calls.append(a)),
                patch.object(cw, 'handle', side_effect=store_and_stop),
            ):
                result = cw.main()
        finally:
            flowing.set()
        client.unsubscribe.assert_called_once_with(cfg['TOPICS'])
        self.assertEqual(result, 1)

if __name__ == '__main__':
    unittest.main()
