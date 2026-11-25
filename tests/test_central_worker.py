"""중앙 수집 워커 검사/저장 로직 시험. MQTT·MySQL에 실제로 연결하지 않는다."""
import json
import unittest
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


if __name__ == '__main__':
    unittest.main()
