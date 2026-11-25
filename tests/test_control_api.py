"""통제 API 시험. 실제 MQTT·MySQL에 연결하지 않는다(가짜 저장소·발행기·paho 클라이언트를 쓴다).

- 명령 형식이 시설 controller의 검사(shape, command_reason, time_reason)를 그대로 통과하는지 확인한다.
- gunicorn 기동 시험은 gunicorn·paho·prometheus_client가 설치된 Linux(CI)에서만 돈다.
"""
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.coldguard_protocol import command_reason, time_reason
from control_api import app as api
from control_api import config as config_module
from control_api.auth import KeyRing, bearer
from control_api.bus import NotConnected, Publisher, PublishUnconfirmed
from control_api.store import Store
from controller.controller_svc import shape

NOW = datetime(2026, 11, 26, 1, 0, 0, 123456, tzinfo=timezone.utc)
NOW_NAIVE = datetime(2026, 11, 26, 1, 0, 0, 123456)
CMD_KEY, READ_KEY, SITE2_KEY = 'c' * 40, 'r' * 40, 's' * 40
ENV = {'MQTT_PASS': 'mqtt-secret', 'MQTT_CLIENT_ID': 'control-api-test', 'MYSQL_PASSWORD': 'db-secret',
       'SITES': 'site01,site02'}
KEYS = [
    {'name': 'kt-ui', 'key': CMD_KEY, 'scope': 'command', 'sites': ['site01']},
    {'name': 'kt-load', 'key': READ_KEY, 'scope': 'read', 'sites': ['site01']},
    {'name': 'site2-ops', 'key': SITE2_KEY, 'scope': 'command', 'sites': ['site02']},
]
# 배포 매니페스트(coldguard-deploy control-api/deployment.yaml)의 gunicorn 인자와 같게 유지한다.
GUNICORN_ARGS = ['--bind', '0.0.0.0:8000', '--workers', '1', '--worker-class', 'gthread', '--threads', '8',
                 '--worker-tmp-dir', '/tmp', '--graceful-timeout', '20', '--no-control-socket']


def make_cfg(**over):
    return config_module.load({**ENV, **over})


# ---------- 가짜 객체 ----------

class FakeStore:
    def __init__(self):
        self.calls, self.fail = [], None
        self.reqs, self.ack_rows = [], []
        self.latest_rows, self.series_rows, self.event_rows = [], [], []
        self.args = {}

    def _call(self, name, *args):
        self.calls.append(name)
        self.args[name] = args
        if self.fail:
            raise self.fail

    def ping(self):
        self._call('ping')

    def insert_request(self, row):
        self._call('insert', row)
        self.reqs.append(dict(row))

    def request(self, cmd_id, sites):
        self._call('request', cmd_id, sites)
        return next((r for r in self.reqs if r['cmd_id'] == cmd_id and r['site_id'] in sites), None)

    def requests(self, sites, device_id, limit):
        self._call('requests', sites, device_id, limit)
        rows = [r for r in self.reqs if r['site_id'] in sites and (not device_id or r['device_id'] == device_id)]
        return sorted(rows, key=lambda r: r['issued_at'], reverse=True)[:limit]

    def acks(self, cmd_ids):
        self._call('acks', cmd_ids)
        return [a for a in self.ack_rows if a['cmd_id'] in cmd_ids]

    def latest(self, sites):
        self._call('latest', sites)
        return self.latest_rows

    def series(self, site_id, device_id, since, step):
        self._call('series', site_id, device_id, since, step)
        return self.series_rows

    def events(self, sites, device_id, since, limit):
        self._call('events', sites, device_id, since, limit)
        return self.event_rows


class FakePublisher:
    def __init__(self, store):
        self.store, self.up, self.fail, self.sent = store, True, None, []

    def connected(self):
        return self.up

    def publish(self, topic, body):
        self.store.calls.append('publish')
        if self.fail:
            raise self.fail
        self.sent.append((topic, json.loads(json.dumps(body, allow_nan=False))))


class FakeMetrics:
    def __init__(self):
        self.requests, self.commands, self.auth, self.db_states = [], [], [], []

    def request(self, endpoint, method, code, seconds):
        self.requests.append((endpoint, method, code))

    def command(self, result):
        self.commands.append(result)

    def auth_failure(self, reason):
        self.auth.append(reason)

    def mqtt(self, up):
        pass

    def db(self, up):
        self.db_states.append(up)


def ack_row(cmd_id, result='accepted', reason=None, at=None, device='fz01', site='site01', row_id=1):
    return dict(id=row_id, cmd_id=cmd_id, site_id=site, device_id=device, result=result, reason=reason,
                acked_at=at or datetime(2026, 11, 26, 1, 0, 1, 500000))


def req_row(cmd_id='11111111-1111-4111-8111-111111111111', site='site01', device='fz01',
            issued=datetime(2026, 11, 26, 0, 59, 30), ttl=60):
    return dict(cmd_id=cmd_id, site_id=site, device_id=device, action='set_temp', value=Decimal('-22.00'),
                issued_by='kt-ui', issued_at=issued, expires_at=issued + timedelta(seconds=ttl))


class ApiCase(unittest.TestCase):
    def setUp(self):
        log_patch = patch('control_api.app.log')
        self.log = log_patch.start()
        self.addCleanup(log_patch.stop)
        self.cfg = make_cfg()
        self.store = FakeStore()
        self.pub = FakePublisher(self.store)
        self.metrics = FakeMetrics()
        self.now = NOW
        self.app = api.create_app(cfg=self.cfg, store=self.store, publisher=self.pub,
                                  keys=KeyRing(KEYS, self.cfg['SITES']), metrics=self.metrics,
                                  clock=lambda: self.now)
        self.client = self.app.test_client()

    def headers(self, key):
        return {'Authorization': 'Bearer ' + key} if key else {}

    def post(self, body=None, key=CMD_KEY, raw=None, content_type='application/json'):
        data = raw if raw is not None else json.dumps(body, ensure_ascii=False)
        return self.client.post('/api/v1/commands', data=data, headers=self.headers(key),
                                content_type=content_type)

    def get(self, path, key=CMD_KEY):
        return self.client.get(path, headers=self.headers(key))

    def logged(self, event):
        return [c for c in self.log.call_args_list if c.args and c.args[0] == event]


# ---------- 설정 ----------

class ConfigTests(unittest.TestCase):
    def test_defaults_match_cluster(self):
        cfg = make_cfg()
        self.assertEqual((cfg['MQTT_HOST'], cfg['MQTT_PORT'], cfg['MQTT_USER'], cfg['MQTT_TLS']),
                         ('mosquitto', 1883, 'control-api', False))
        self.assertEqual((cfg['MYSQL_HOST'], cfg['MYSQL_DB'], cfg['MYSQL_USER']),
                         ('mysql', 'coldguard_central', 'coldguard_api'))
        self.assertEqual(cfg['SITES'], ('site01', 'site02'))
        self.assertEqual(cfg['CMD_TTL_SEC'], 60.0)
        self.assertEqual(cfg['METRICS_PORT'], 9204)

    def test_required_values(self):
        for key in ('MQTT_PASS', 'MQTT_CLIENT_ID', 'MYSQL_PASSWORD', 'SITES'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                make_cfg(**{key: ''})

    def test_tls_requires_ca(self):
        with self.assertRaises(ValueError):
            make_cfg(MQTT_TLS='true')
        self.assertTrue(make_cfg(MQTT_TLS='true', MQTT_CA='/etc/ca.crt')['MQTT_TLS'])
        with self.assertRaises(ValueError):
            make_cfg(MQTT_TLS='yes')

    def test_sites_follow_common_id_rule(self):
        for sites in ('Site01', 'site-1', 'site01,site01', ' , ', 'site01,fz01'):
            with self.subTest(sites=sites), self.assertRaises(ValueError):
                make_cfg(SITES=sites)
        self.assertEqual(make_cfg(SITES=' site01 , site02 ')['SITES'], ('site01', 'site02'))

    def test_ttl_within_controller_limit(self):
        for ttl in ('0', '-1', '300.5', 'nan', 'inf'):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                make_cfg(CMD_TTL_SEC=ttl)
        self.assertEqual(make_cfg(CMD_TTL_SEC='300')['CMD_TTL_SEC'], 300.0)

    def test_ports(self):
        for key in ('MQTT_PORT', 'MYSQL_PORT', 'METRICS_PORT'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                make_cfg(**{key: '70000'})


# ---------- API 키 ----------

class AuthTests(unittest.TestCase):
    def test_lookup(self):
        ring = KeyRing(KEYS, ('site01', 'site02'))
        self.assertEqual(ring.lookup(CMD_KEY).name, 'kt-ui')
        self.assertEqual(ring.lookup(READ_KEY).scope, 'read')
        self.assertIsNone(ring.lookup('x' * 40))
        self.assertIsNone(ring.lookup('ㄱ' * 40))
        self.assertIsNone(ring.lookup(None))
        self.assertEqual([p.name for p in ring.principals()], ['kt-ui', 'kt-load', 'site2-ops'])

    def test_invalid_entries_stop_startup(self):
        good = dict(KEYS[0])
        bad = [
            [], {'not': 'list'},
            [{**good, 'key': 'short'}],
            [{**good, 'name': 'KT-UI'}],
            [{**good, 'name': 'a' * 20}],
            [{**good, 'scope': 'admin'}],
            [{**good, 'sites': ['site03']}],
            [{**good, 'sites': []}],
            [{**good, 'sites': ['site01', 'site01']}],
            [{**good, 'extra': 1}],
            [good, {**good, 'key': 'd' * 40}],
            [good, {**good, 'name': 'other'}],
        ]
        for entries in bad:
            with self.subTest(entries=str(entries)[:60]), self.assertRaises(ValueError):
                KeyRing(entries, ('site01', 'site02'))

    def test_error_message_does_not_contain_key(self):
        with self.assertRaises(ValueError) as ctx:
            KeyRing([{**KEYS[0], 'scope': 'admin'}], ('site01',))
        self.assertNotIn(CMD_KEY, str(ctx.exception))

    def test_bearer_header(self):
        self.assertEqual(bearer('Bearer abc'), 'abc')
        self.assertEqual(bearer('bearer  abc '), 'abc')
        for header in (None, '', 'Basic abc', 'Bearer', 'Bearer   ', 'abc'):
            with self.subTest(header=header):
                self.assertIsNone(bearer(header))

    def test_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'keys.json'
            path.write_text(json.dumps(KEYS), encoding='utf-8')
            self.assertEqual(KeyRing.from_file(str(path), ('site01', 'site02')).lookup(SITE2_KEY).name,
                             'site2-ops')


# ---------- 명령 발행 ----------

class CommandTests(ApiCase):
    def test_accepted_request_records_then_publishes_controller_compatible_cmd(self):
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -22.0, 'operator': '관제01'})
        self.assertEqual(r.status_code, 202, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(self.store.calls, ['insert', 'publish'])  # 기록이 먼저
        topic, cmd = self.pub.sent[0]
        self.assertEqual(topic, 'coldguard/site01/fz01/cmd')
        self.assertEqual(set(cmd), {'site_id', 'device_id', 'cmd_id', 'action', 'value', 'issued_at',
                                    'expires_at', 'issued_by'})
        self.assertEqual(cmd['action'], 'set_temp')
        self.assertEqual(cmd['value'], -22.0)
        self.assertIsInstance(cmd['value'], float)
        self.assertEqual(cmd['issued_by'], 'kt-ui:관제01')
        self.assertEqual(cmd['issued_at'], '2026-11-26T01:00:00.123Z')
        self.assertEqual(cmd['expires_at'], '2026-11-26T01:01:00.123Z')
        self.assertEqual(str(uuid.UUID(cmd['cmd_id'])), cmd['cmd_id'])
        # 시설 controller의 실제 검사 함수를 통과한다.
        shape(cmd)
        self.assertIsNone(time_reason(cmd, now=NOW))
        self.assertIsNone(command_reason(cmd, topic, 'site01', {'fz01'}, now=NOW))
        # DB 기록은 메시지와 같은 값(밀리초 UTC)이다.
        row = self.store.reqs[0]
        self.assertEqual(row['cmd_id'], cmd['cmd_id'])
        self.assertEqual(row['value'], Decimal('-22.0'))
        self.assertEqual(row['issued_at'], datetime(2026, 11, 26, 1, 0, 0, 123000))
        self.assertEqual(row['expires_at'], datetime(2026, 11, 26, 1, 1, 0, 123000))
        self.assertEqual(row['issued_by'], 'kt-ui:관제01')
        # 응답
        self.assertEqual(r.headers['Location'], '/api/v1/commands/' + cmd['cmd_id'])
        self.assertEqual((body['status'], body['issued_at'], body['value']),
                         ('pending', cmd['issued_at'], -22.0))
        self.assertIn('관제01', r.get_data(as_text=True))  # 한글을 \u 이스케이프하지 않는다
        self.assertEqual(self.metrics.commands, ['sent'])
        self.assertEqual(len(self.logged('command_sent')), 1)

    def test_integer_value_and_no_operator(self):
        r = self.post({'site_id': 'site01', 'device_id': 'fz02', 'value': -20})
        self.assertEqual(r.status_code, 202)
        cmd = self.pub.sent[0][1]
        self.assertEqual((cmd['value'], cmd['issued_by']), (-20.0, 'kt-ui'))
        shape(cmd)

    def test_range_is_judged_by_controller_not_api(self):
        # 범위 밖 값도 시설까지 전달된다. 판정은 controller의 out_of_range.
        for value, expected in ((-60, 'out_of_range'), (-10, 'out_of_range'), (-50, None), (-10.01, None)):
            with self.subTest(value=value):
                self.pub.sent.clear()
                r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': value})
                self.assertEqual(r.status_code, 202)
                topic, cmd = self.pub.sent[0]
                self.assertEqual(command_reason(cmd, topic, 'site01', {'fz01'}, now=NOW), expected)

    def test_unregistered_device_is_sent_for_controller_to_reject(self):
        r = self.post({'site_id': 'site01', 'device_id': 'fz99', 'value': -22})
        self.assertEqual(r.status_code, 202)
        topic, cmd = self.pub.sent[0]
        self.assertEqual(command_reason(cmd, topic, 'site01', {'fz01'}, now=NOW), 'unknown_device')

    def test_invalid_values_are_not_sent(self):
        for value in ('-20', True, None, [], {}, -20.123, 1000, -1000.0, 1e300):
            with self.subTest(value=value):
                r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': value})
                self.assertEqual((r.status_code, r.get_json()['error']), (400, 'invalid_value'))
        self.assertEqual(self.store.calls, [])

    def test_invalid_json_bodies(self):
        cases = [('{"site_id":"site01","device_id":"fz01","value":NaN}', 'invalid_json'),
                 ('{"site_id":"site01","site_id":"site01","device_id":"fz01","value":-20}', 'invalid_json'),
                 ('[1,2]', 'invalid_json'), ('{bad', 'invalid_json'),
                 ('{"site_id":"site01","device_id":"fz01","value":1e400}', 'invalid_value')]
        for raw, error in cases:
            with self.subTest(raw=raw):
                r = self.post(raw=raw)
                self.assertEqual((r.status_code, r.get_json()['error']), (400, error))
        self.assertEqual(self.store.calls, [])

    def test_fields(self):
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20, 'action': 'set_temp'})
        self.assertEqual((r.status_code, r.get_json()), (400, {'error': 'unknown_field', 'fields': ['action']}))
        r = self.post({'site_id': 'site01', 'device_id': 'fz01'})
        self.assertEqual((r.status_code, r.get_json()), (400, {'error': 'missing_field', 'fields': ['value']}))

    def test_operator_rules(self):
        for operator in ('', '   ', 'a' * 31, '관제\n01', 5):
            with self.subTest(operator=operator):
                r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20, 'operator': operator})
                self.assertEqual((r.status_code, r.get_json()['error']), (400, 'invalid_operator'))
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20, 'operator': ' ' + 'a' * 30 + ' '})
        self.assertEqual(r.status_code, 202)
        cmd = self.pub.sent[0][1]
        self.assertEqual(len(cmd['issued_by']), len('kt-ui:') + 30)
        shape(cmd)  # issued_by 50자 제한 안

    def test_sites_and_devices(self):
        cases = [({'site_id': 'site03'}, 400, 'unknown_site'), ({'site_id': 7}, 400, 'unknown_site'),
                 ({'site_id': 'site02'}, 403, 'forbidden'), ({'device_id': 'FZ01'}, 400, 'invalid_device_id'),
                 ({'device_id': 'fz'}, 400, 'invalid_device_id'), ({'device_id': 'fz01/+'}, 400, 'invalid_device_id'),
                 ({'device_id': 1}, 400, 'invalid_device_id')]
        for over, status, error in cases:
            with self.subTest(over=over):
                r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20, **over})
                self.assertEqual((r.status_code, r.get_json()['error']), (status, error))
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.metrics.auth, ['site'])

    def test_key_limited_to_other_site(self):
        r = self.post({'site_id': 'site02', 'device_id': 'fz01', 'value': -20}, key=SITE2_KEY)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.pub.sent[0][0], 'coldguard/site02/fz01/cmd')
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20}, key=SITE2_KEY)
        self.assertEqual(r.status_code, 403)

    def test_authentication(self):
        body = {'site_id': 'site01', 'device_id': 'fz01', 'value': -20}
        r = self.post(body, key=None)
        self.assertEqual((r.status_code, r.headers.get('WWW-Authenticate')), (401, 'Bearer'))
        self.assertEqual(self.post(body, key='x' * 40).status_code, 401)
        r = self.post(body, key=READ_KEY)
        self.assertEqual((r.status_code, r.get_json()['error']), (403, 'forbidden'))
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.metrics.auth, ['missing', 'invalid', 'scope'])
        rejected = self.logged('control_api_auth_rejected')
        self.assertEqual([c.kwargs['reason'] for c in rejected], ['missing', 'invalid', 'scope'])
        self.assertNotIn('x' * 40, str(rejected))  # 틀린 키도 로그에 남기지 않는다

    def test_content_type_and_size(self):
        self.assertEqual(self.post(raw='site_id=site01', content_type='text/plain').status_code, 415)
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20, 'operator': 'a' * 5000})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.store.calls, [])

    def test_chunked_body_reaching_limit_is_rejected_not_truncated(self):
        # gunicorn은 chunked 본문에 wsgi.input_terminated를 켠다. Werkzeug는 이때 상한에서 조용히 자르므로,
        # 앞 4KB만으로 올바른 JSON이 되게 만든 요청도 처리하지 않고 413으로 거부해야 한다.
        head = json.dumps({'site_id': 'site01', 'device_id': 'fz01', 'value': -20}).encode()
        padded = head + b' ' * (api.MAX_BODY - len(head)) + b'trailing-data'
        r = self.client.post('/api/v1/commands', input_stream=io.BytesIO(padded),
                             headers={**self.headers(CMD_KEY), 'Content-Type': 'application/json',
                                      'Transfer-Encoding': 'chunked'},
                             environ_overrides={'wsgi.input_terminated': True})
        self.assertEqual((r.status_code, r.get_json()['error']), (413, 'request_entity_too_large'))
        self.assertEqual(self.store.calls, [])
        small = io.BytesIO(head)
        r = self.client.post('/api/v1/commands', input_stream=small,
                             headers={**self.headers(CMD_KEY), 'Content-Type': 'application/json',
                                      'Transfer-Encoding': 'chunked'},
                             environ_overrides={'wsgi.input_terminated': True})
        self.assertEqual(r.status_code, 202)

    def test_mqtt_down_before_record(self):
        self.pub.up = False
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20})
        self.assertEqual((r.status_code, r.get_json()), (503, {'error': 'mqtt_unavailable', 'sent': False}))
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.metrics.commands, ['mqtt_unavailable'])

    def test_db_failure_does_not_publish(self):
        self.store.fail = RuntimeError('db down')
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20})
        self.assertEqual((r.status_code, r.get_json()['error']), (503, 'db_unavailable'))
        self.assertEqual(self.store.calls, ['insert'])
        self.assertEqual(self.pub.sent, [])
        self.assertEqual(self.metrics.db_states, [False])

    def test_connection_lost_between_record_and_publish(self):
        self.pub.fail = NotConnected()
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20})
        body = r.get_json()
        self.assertEqual((r.status_code, body['error'], body['sent']), (503, 'mqtt_unavailable', False))
        self.assertEqual(body['cmd_id'], self.store.reqs[0]['cmd_id'])
        self.assertEqual(len(self.logged('command_not_sent')), 1)

    def test_publish_unconfirmed_returns_cmd_id(self):
        self.pub.fail = PublishUnconfirmed('timeout')
        r = self.post({'site_id': 'site01', 'device_id': 'fz01', 'value': -20})
        body = r.get_json()
        self.assertEqual((r.status_code, body['error']), (503, 'publish_unconfirmed'))
        self.assertEqual(body['cmd_id'], self.store.reqs[0]['cmd_id'])
        self.assertEqual(self.metrics.commands, ['publish_unconfirmed'])


# ---------- 명령 상태 ----------

class StatusTests(unittest.TestCase):
    def status(self, acks, now=NOW, **req):
        return api.command_status(req_row(**req), acks, now)

    def test_pending_then_no_response(self):
        self.assertEqual(self.status([])['status'], 'pending')
        after = datetime(2026, 11, 26, 1, 0, 30, tzinfo=timezone.utc)  # 만료(01:00:30) 시각
        self.assertEqual(self.status([], now=after)['status'], 'no_response')

    def test_late_ack_after_reconnect(self):
        late = datetime(2026, 11, 26, 1, 3, tzinfo=timezone.utc)
        req = req_row()
        s = api.command_status(req, [ack_row(req['cmd_id'], 'rejected', 'expired',
                                             at=datetime(2026, 11, 26, 1, 2, 50))], late)
        self.assertEqual((s['status'], s['reason'], s['decided_at']),
                         ('rejected', 'expired', '2026-11-26T01:02:50.000Z'))

    def test_first_decision_wins_and_replays_are_counted(self):
        cid = req_row()['cmd_id']
        accepted = ack_row(cid, at=datetime(2026, 11, 26, 1, 0, 1), row_id=1)
        dup1 = ack_row(cid, 'rejected', 'duplicate', at=datetime(2026, 11, 26, 1, 5), row_id=2)
        dup2 = ack_row(cid, 'rejected', 'duplicate', at=datetime(2026, 11, 26, 1, 6), row_id=3)
        s = self.status([dup2, dup1, accepted])  # 순서가 섞여 와도 시각 순으로 판단
        self.assertEqual((s['status'], s['reason'], s['replay_rejected']), ('accepted', None, 2))

    def test_only_duplicate(self):
        cid = req_row()['cmd_id']
        s = self.status([ack_row(cid, 'rejected', 'duplicate'),
                         ack_row(cid, 'rejected', 'duplicate', at=datetime(2026, 11, 26, 1, 1), row_id=2)])
        self.assertEqual((s['status'], s['reason'], s['replay_rejected']), ('rejected', 'duplicate', 1))

    def test_ack_from_other_device_is_ignored(self):
        cid = req_row()['cmd_id']
        self.assertEqual(self.status([ack_row(cid, device='fz02'), ack_row(cid, site='site02')])['status'],
                         'pending')

    def test_rejections(self):
        cid = req_row()['cmd_id']
        for reason in ('locked', 'out_of_range', 'unknown_device', 'apply_timeout', 'expired'):
            with self.subTest(reason=reason):
                s = self.status([ack_row(cid, 'rejected', reason)])
                self.assertEqual((s['status'], s['reason']), ('rejected', reason))


# ---------- 조회 ----------

class ReadTests(ApiCase):
    def add_command(self, **over):
        row = req_row(**over)
        self.store.reqs.append(row)
        return row['cmd_id']

    def test_get_command_with_result(self):
        cid = self.add_command()
        self.store.ack_rows.append(ack_row(cid, 'rejected', 'out_of_range'))
        r = self.get('/api/v1/commands/' + cid, key=READ_KEY)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual((body['status'], body['reason'], body['value'], body['issued_at']),
                         ('rejected', 'out_of_range', -22.0, '2026-11-26T00:59:30.000Z'))

    def test_get_command_not_found_or_other_site(self):
        cid = self.add_command()
        self.assertEqual(self.get('/api/v1/commands/' + cid, key=SITE2_KEY).status_code, 404)
        self.assertEqual(self.get('/api/v1/commands/unknown-id').status_code, 404)
        self.assertEqual(self.get('/api/v1/commands/' + 'a' * 51).status_code, 404)
        self.assertEqual(self.get('/api/v1/commands/bad_id').status_code, 404)

    def test_list_commands(self):
        first = self.add_command(cmd_id='a' * 36, issued=datetime(2026, 11, 26, 0, 50))
        second = self.add_command(cmd_id='b' * 36, issued=datetime(2026, 11, 26, 0, 59, 50), device='fz02')
        self.store.ack_rows.append(ack_row(first))
        r = self.get('/api/v1/commands')
        self.assertEqual(r.status_code, 200)
        items = r.get_json()['commands']
        self.assertEqual([(c['cmd_id'], c['status']) for c in items],
                         [(second, 'pending'), (first, 'accepted')])
        self.assertEqual(self.store.args['requests'], (('site01',), None, 50))
        self.get('/api/v1/commands?device_id=fz02&limit=200&site_id=site01')
        self.assertEqual(self.store.args['requests'], (('site01',), 'fz02', 200))

    def test_list_parameters(self):
        for query, status in (('limit=0', 400), ('limit=201', 400), ('limit=abc', 400), ('limit=-1', 400),
                              ('limit=１', 400), ('site_id=site03', 400), ('site_id=site02', 403),
                              ('device_id=FZ1', 400)):
            with self.subTest(query=query):
                self.assertEqual(self.get('/api/v1/commands?' + query).status_code, status)

    def test_devices(self):
        self.store.latest_rows = [dict(site_id='site01', device_id='fz01', seq=15243,
                                       measured_at=datetime(2026, 11, 26, 0, 59, 58), temp_c=Decimal('-29.80'),
                                       setpoint_c=Decimal('-30.00'), door_open=0, cooling=1, status='normal',
                                       received_at=datetime(2026, 11, 26, 0, 59, 58, 400000))]
        r = self.get('/api/v1/devices', key=READ_KEY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['devices'], [dict(
            site_id='site01', device_id='fz01', seq=15243, measured_at='2026-11-26T00:59:58.000Z',
            received_at='2026-11-26T00:59:58.400Z', age_sec=2.123, temp_c=-29.8, setpoint_c=-30.0,
            door_open=False, cooling=True, status='normal')])
        self.assertEqual(self.store.args['latest'], (('site01',),))

    def test_telemetry_series(self):
        self.store.series_rows = [dict(bucket=Decimal(int(datetime(2026, 11, 26, 0, 30, tzinfo=timezone.utc)
                                                          .timestamp()) // 5),
                                       temp_avg=Decimal('-29.812345'), temp_min=Decimal('-30.10'),
                                       temp_max=Decimal('-29.50'), setpoint_c='-30.00', door_open=1, samples=5)]
        r = self.get('/api/v1/devices/site01/fz01/telemetry', key=READ_KEY)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual((body['minutes'], body['step_sec']), (60, 5))
        self.assertEqual(body['points'], [dict(t='2026-11-26T00:30:00.000Z', temp_avg=-29.81, temp_min=-30.1,
                                               temp_max=-29.5, setpoint_c=-30.0, door_open=True, samples=5)])
        self.assertEqual(self.store.args['series'],
                         ('site01', 'fz01', NOW_NAIVE - timedelta(minutes=60), 5))
        for minutes, step in (('10', 1), ('1440', 120), ('61', 6)):
            with self.subTest(minutes=minutes):
                self.get('/api/v1/devices/site01/fz01/telemetry?minutes=' + minutes)
                self.assertEqual(self.store.args['series'][3], step)

    def test_telemetry_parameters(self):
        for path, status in (('site01/fz01/telemetry?minutes=0', 400), ('site01/fz01/telemetry?minutes=1441', 400),
                             ('site01/FZ01/telemetry', 400), ('site02/fz01/telemetry', 403),
                             ('site03/fz01/telemetry', 400)):
            with self.subTest(path=path):
                self.assertEqual(self.get('/api/v1/devices/' + path).status_code, status)

    def test_events(self):
        self.store.event_rows = [dict(id=7, site_id='site01', device_id='fz01', event_type='cmd_rejected',
                                      severity='warning', temp_c=None, predicted_sec=None,
                                      detail='abc / out_of_range', occurred_at=datetime(2026, 11, 26, 0, 58),
                                      received_at=datetime(2026, 11, 26, 0, 58, 0, 300000))]
        r = self.get('/api/v1/events?device_id=fz01', key=READ_KEY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['events'][0]['occurred_at'], '2026-11-26T00:58:00.000Z')
        self.assertEqual(self.store.args['events'],
                         (('site01',), 'fz01', NOW_NAIVE - timedelta(minutes=1440), 100))
        self.assertEqual(self.get('/api/v1/events?limit=501').status_code, 400)
        self.assertEqual(self.get('/api/v1/events?minutes=10081').status_code, 400)

    def test_me(self):
        self.assertEqual(self.get('/api/v1/me', key=READ_KEY).get_json(),
                         {'name': 'kt-load', 'scope': 'read', 'sites': ['site01']})
        self.assertEqual(self.get('/api/v1/me', key=None).status_code, 401)

    def test_db_error_is_503(self):
        self.store.fail = RuntimeError('db down')
        r = self.get('/api/v1/devices')
        self.assertEqual((r.status_code, r.get_json()), (503, {'error': 'db_unavailable'}))

    def test_unexpected_error_is_500_without_details(self):
        self.store.latest_rows = [{'site_id': 'site01'}]  # 필드가 빠진 이상한 행
        r = self.get('/api/v1/devices')
        self.assertEqual((r.status_code, r.get_json()), (500, {'error': 'internal_error'}))

    def test_unknown_route_and_method(self):
        self.assertEqual(self.get('/api/v1/nothing').get_json(), {'error': 'not_found'})
        r = self.client.delete('/api/v1/commands', headers=self.headers(CMD_KEY))
        self.assertEqual(r.status_code, 405)


class HealthTests(ApiCase):
    def test_healthz_needs_no_key_and_no_backend(self):
        self.store.fail = RuntimeError('db down')
        self.pub.up = False
        r = self.client.get('/healthz')
        self.assertEqual((r.status_code, r.get_json()), (200, {'status': 'ok'}))

    def test_readyz_follows_db(self):
        r = self.client.get('/readyz')
        self.assertEqual((r.status_code, r.get_json()), (200, {'status': 'ready', 'db': True, 'mqtt': True}))
        self.pub.up = False
        self.assertEqual(self.client.get('/readyz').get_json()['mqtt'], False)
        self.store.fail = RuntimeError('db down')
        r = self.client.get('/readyz')
        self.assertEqual((r.status_code, r.get_json()['db']), (503, False))

    def test_request_metrics_use_route_patterns(self):
        cid = req_row()['cmd_id']
        self.store.reqs.append(req_row())
        self.get('/api/v1/commands/' + cid)
        self.client.get('/healthz')
        self.assertIn(('/api/v1/commands/<cmd_id>', 'GET', 200), self.metrics.requests)
        self.assertIn(('/healthz', 'GET', 200), self.metrics.requests)

    def test_security_headers(self):
        r = self.client.get('/healthz')
        self.assertEqual((r.headers['Cache-Control'], r.headers['X-Content-Type-Options']), ('no-store', 'nosniff'))


class StartupTests(unittest.TestCase):
    def test_missing_key_file_stops_startup(self):
        env = {**ENV, 'API_KEYS_FILE': '/nonexistent/keys.json'}
        with patch.dict(os.environ, env, clear=True), patch('control_api.app.log') as log:
            with self.assertRaises(OSError):
                api.create_app()
        self.assertEqual(log.call_args.args[0], 'control_api_config_error')

    def test_bad_config_stops_startup(self):
        with patch.dict(os.environ, {}, clear=True), patch('control_api.app.log') as log:
            with self.assertRaises(ValueError):
                api.create_app()
        self.assertIn('MQTT_PASS', log.call_args.kwargs['reason'])
        self.assertNotIn('secret', log.call_args.kwargs['reason'])


# ---------- MQTT 발행기 ----------

class FakeInfo:
    def __init__(self, rc=0, published=True, error=None):
        self.rc, self._published, self._error = rc, published, error

    def wait_for_publish(self, timeout):
        if self._error:
            raise self._error

    def is_published(self):
        return self._published


class FakeClient:
    def __init__(self):
        self.info, self.published, self.events = FakeInfo(), [], []

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return self.info

    def connect_async(self, host, port, keepalive):
        self.events.append(('connect', host, port, keepalive))

    def loop_start(self):
        self.events.append('loop_start')

    def disconnect(self):
        self.events.append('disconnect')

    def loop_stop(self):
        self.events.append('loop_stop')


OK = SimpleNamespace(is_failure=False)
FAIL = SimpleNamespace(is_failure=True)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        log_patch = patch('control_api.bus.log')
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.states = []
        self.client = FakeClient()
        self.pub = Publisher(make_cfg(), on_state=self.states.append, client=self.client)

    def connect(self):
        self.client.on_connect(self.client, None, None, OK, None)

    def test_start_uses_cluster_broker(self):
        self.pub.start()
        self.assertEqual(self.client.events, [('connect', 'mosquitto', 1883, 60), 'loop_start'])

    def test_not_connected_does_not_publish(self):
        with self.assertRaises(NotConnected):
            self.pub.publish('coldguard/site01/fz01/cmd', {'a': 1})
        self.assertEqual(self.client.published, [])

    def test_publish_qos1_without_retain(self):
        self.connect()
        self.pub.publish('coldguard/site01/fz01/cmd', {'issued_by': '관제'})
        topic, payload, qos, retain = self.client.published[0]
        self.assertEqual((topic, qos, retain), ('coldguard/site01/fz01/cmd', 1, False))
        self.assertEqual(json.loads(payload), {'issued_by': '관제'})
        self.assertEqual(self.states, [True])

    def test_unconfirmed_cases(self):
        self.connect()
        for info in (FakeInfo(rc=4), FakeInfo(published=False), FakeInfo(error=RuntimeError('no conn')),
                     FakeInfo(error=ValueError('queue'))):
            with self.subTest(rc=info.rc), self.assertRaises(PublishUnconfirmed):
                self.client.info = info
                self.pub.publish('coldguard/site01/fz01/cmd', {})

    def test_connection_state(self):
        self.connect()
        self.assertTrue(self.pub.connected())
        self.client.on_disconnect(self.client, None, None, FAIL, None)
        self.assertFalse(self.pub.connected())
        self.client.on_connect(self.client, None, None, FAIL, None)
        self.assertFalse(self.pub.connected())
        self.assertEqual(self.states, [True, False, False])

    def test_stop(self):
        self.pub.stop()
        self.assertEqual(self.client.events, ['disconnect', 'loop_stop'])


# ---------- SQL ----------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=()):
        self.conn.executed.append((sql, tuple(args)))
        if self.conn.fail:
            raise self.conn.fail

    def fetchall(self):
        return self.conn.rows


class FakeConn:
    def __init__(self, rows=None, fail=None):
        self.rows, self.fail, self.executed = rows or [], fail, []
        self.committed = self.rolled_back = self.closed = False

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class StoreTests(unittest.TestCase):
    def run_store(self, method, *args, rows=None):
        conn = FakeConn(rows)
        store = Store(make_cfg())
        with patch.object(Store, 'connect', return_value=conn):
            result = getattr(store, method)(*args)
        return conn, result

    def assert_placeholders(self, conn):
        for sql, args in conn.executed:
            self.assertEqual(sql.count('%s'), len(args), sql)
            if sql.startswith('SELECT') and 'SELECT 1' not in sql:
                self.assertTrue(sql.startswith('SELECT /*+ MAX_EXECUTION_TIME(5000) */ '), sql)

    def test_queries_bind_every_value(self):
        since = datetime(2026, 11, 26)
        calls = [('request', 'cid', ('site01', 'site02')), ('requests', ('site01',), None, 50),
                 ('requests', ('site01', 'site02'), 'fz01', 10), ('acks', ['a', 'b', 'c']),
                 ('latest', ('site01', 'site02')), ('series', 'site01', 'fz01', since, 5),
                 ('events', ('site01',), None, since, 100), ('events', ('site01',), 'fz02', since, 100), ('ping',)]
        for method, *args in calls:
            with self.subTest(method=method, args=args):
                conn, _ = self.run_store(method, *args)
                self.assertEqual(len(conn.executed), 1)
                self.assert_placeholders(conn)
                self.assertTrue(conn.closed)

    def test_requests_filters_and_limit_last(self):
        conn, _ = self.run_store('requests', ('site01',), 'fz01', 10)
        sql, args = conn.executed[0]
        self.assertIn('AND device_id=%s', sql)
        self.assertTrue(sql.endswith('LIMIT %s'))
        self.assertEqual(args, ('site01', 'fz01', 10))

    def test_no_ack_query_for_empty_list(self):
        conn, result = self.run_store('acks', [])
        self.assertEqual((conn.executed, result), ([], []))

    def test_latest_keeps_highest_seq_per_device(self):
        t = datetime(2026, 11, 26)
        rows = [dict(site_id='site01', device_id='fz01', seq=9, measured_at=t),
                dict(site_id='site01', device_id='fz01', seq=8, measured_at=t),
                dict(site_id='site01', device_id='fz02', seq=3, measured_at=t)]
        _, result = self.run_store('latest', ('site01',), rows=rows)
        self.assertEqual([(r['device_id'], r['seq']) for r in result], [('fz01', 9), ('fz02', 3)])

    def test_insert_commits(self):
        row = dict(cmd_id='a', site_id='site01', device_id='fz01', action='set_temp', value=Decimal('-22.00'),
                   issued_by='kt-ui', issued_at=datetime(2026, 11, 26), expires_at=datetime(2026, 11, 26, 0, 1))
        conn, _ = self.run_store('insert_request', row)
        self.assert_placeholders(conn)
        sql, args = conn.executed[0]
        self.assertTrue(sql.startswith('INSERT INTO command_request('))
        self.assertEqual(args, ('a', 'site01', 'fz01', 'set_temp', Decimal('-22.00'), 'kt-ui',
                                datetime(2026, 11, 26), datetime(2026, 11, 26, 0, 1)))
        self.assertTrue(conn.committed and conn.closed)

    def test_insert_failure_rolls_back(self):
        conn = FakeConn(fail=RuntimeError('dup'))
        with patch.object(Store, 'connect', return_value=conn), self.assertRaises(RuntimeError):
            Store(make_cfg()).insert_request(dict(cmd_id='a', site_id='s', device_id='d', action='x', value=1,
                                                  issued_by='k', issued_at=None, expires_at=None))
        self.assertTrue(conn.rolled_back and conn.closed and not conn.committed)


# ---------- gunicorn 기동 (CI) ----------

def _installed(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _raw_http(port, data, timeout=10):
    """요청 바이트를 그대로 보내고 (상태 코드, 본문)을 돌려준다. http.client가 고쳐 보내지 못하게 소켓을 직접 쓴다."""
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as s:
        s.sendall(data)
        received = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            received += chunk
    head, _, body = received.partition(b'\r\n\r\n')
    return int(head.split(b' ', 2)[1]), body


def _request(method, path, body=b'', headers=()):
    lines = [f'{method} {path} HTTP/1.1', 'Host: control-api', 'Connection: close', *headers]
    return ('\r\n'.join(lines) + '\r\n\r\n').encode() + body


def _chunked(body, size=16):
    parts = [body[i:i + size] for i in range(0, len(body), size)]
    return b''.join(f'{len(p):x}\r\n'.encode() + p + b'\r\n' for p in parts) + b'0\r\n\r\n'


@unittest.skipUnless(sys.platform.startswith('linux') and all(map(_installed, (
    'gunicorn', 'paho.mqtt.client', 'prometheus_client', 'mysql.connector'))),
    'gunicorn·paho·prometheus_client·mysql-connector가 설치된 Linux(CI)에서만 실행')
class GunicornTests(unittest.TestCase):
    """배포와 같은 gunicorn 인자로 실제 기동한다. MQTT·MySQL은 닫힌 포트를 가리켜 연결되지 않은 상태로 둔다."""

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        keys = self.tmp / 'keys.json'
        keys.write_text(json.dumps(KEYS[:2]), encoding='utf-8')
        self.metrics_port = _free_port()
        self.env = {**os.environ, **ENV, 'API_KEYS_FILE': str(keys), 'METRICS_HOST': '127.0.0.1',
                    'METRICS_PORT': str(self.metrics_port), 'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': '9',
                    'MYSQL_HOST': '127.0.0.1', 'MYSQL_PORT': '9'}

    def test_check_config_loads_factory_with_deploy_arguments(self):
        result = subprocess.run([sys.executable, '-m', 'gunicorn', '--check-config', *GUNICORN_ARGS,
                                 'control_api.app:create_app()'], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('control_api_start', result.stdout)

    def test_server_framing_and_shutdown(self):
        port = _free_port()
        args = list(GUNICORN_ARGS)
        args[args.index('--bind') + 1] = f'127.0.0.1:{port}'  # 같은 인자에서 주소만 바꾼다(--bind를 더하면 둘 다 연다)
        out, err = (open(self.tmp / name, 'w+') for name in ('out.log', 'err.log'))
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        proc = subprocess.Popen([sys.executable, '-m', 'gunicorn', *args, 'control_api.app:create_app()'],
                                cwd=self.root, env=self.env, stdout=out, stderr=err)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        def logs():
            out.seek(0)
            err.seek(0)
            return out.read() + err.read()

        deadline = time.monotonic() + 30
        while True:
            self.assertIsNone(proc.poll(), logs())
            try:
                if _raw_http(port, _request('GET', '/healthz'), timeout=2)[0] == 200:
                    break
            except OSError:
                pass
            self.assertLess(time.monotonic(), deadline, logs())
            time.sleep(0.3)

        auth = (f'Authorization: Bearer {CMD_KEY}', 'Content-Type: application/json')
        body = json.dumps({'site_id': 'site01', 'device_id': 'fz01', 'value': -22}).encode()
        # 본문을 끝까지 읽고 검사까지 통과한 요청만 MQTT 연결 확인(503 mqtt_unavailable)에 닿는다.
        status, data = _raw_http(port, _request('POST', '/api/v1/commands', body,
                                                (*auth, f'Content-Length: {len(body)}')))
        self.assertEqual((status, json.loads(data)['error']), (503, 'mqtt_unavailable'), logs())
        status, data = _raw_http(port, _request('POST', '/api/v1/commands', _chunked(body),
                                                (*auth, 'Transfer-Encoding: chunked')))
        self.assertEqual((status, json.loads(data)['error']), (503, 'mqtt_unavailable'), logs())
        big = json.dumps({'site_id': 'site01', 'device_id': 'fz01', 'value': -22, 'operator': 'a' * 5000}).encode()
        status, data = _raw_http(port, _request('POST', '/api/v1/commands', _chunked(big, 1024),
                                                (*auth, 'Transfer-Encoding: chunked')))
        self.assertEqual((status, json.loads(data)['error']), (413, 'request_entity_too_large'))
        status, _ = _raw_http(port, _request('POST', '/api/v1/commands', body, (f'Content-Length: {len(body)}',)))
        self.assertEqual(status, 401)
        # gunicorn 27의 본문 경계 규칙: 애매한 요청은 앱에 닿기 전에 400으로 끊는다.
        for headers in ((f'Content-Length: {len(body)}', 'Transfer-Encoding: gzip'),
                        (f'Content-Length: {len(body)}', 'Transfer-Encoding: chunked'),
                        (f'Content-Length: {len(body)}', f'Content-Length: {len(body)}')):
            with self.subTest(headers=headers):
                status, _ = _raw_http(port, _request('POST', '/api/v1/commands', body, (*auth, *headers)))
                self.assertEqual(status, 400)
        status, _ = _raw_http(port, _request('GET', '/readyz'))
        self.assertEqual(status, 503)  # DB에 닿지 않으면 준비되지 않음
        status, data = _raw_http(self.metrics_port, _request('GET', '/metrics'))
        self.assertEqual(status, 200)
        self.assertIn(b'coldguard_api_requests_total{', data)
        self.assertIn(b'coldguard_api_commands_total{result="mqtt_unavailable"}', data)

        proc.terminate()  # 쿠버네티스가 파드를 내릴 때와 같은 SIGTERM
        self.assertEqual(proc.wait(timeout=30), 0, logs())
        text = logs()
        self.assertIn('control_api_start', text)
        self.assertNotIn('Traceback', text)
        self.assertNotIn('Control server error', text)  # 제어 소켓을 껐다(--no-control-socket)


if __name__ == '__main__':
    unittest.main()
