"""통제 UI 시험. 실제 통제 API에 연결하지 않는다(가짜 API·시계·지표를 쓴다).

- 로그인·잠금·세션 만료·CSRF·명령 횟수 제한·operator 자동 기록·현장 웹과 같은 장비 표기·API 키 비노출을 확인한다.
- gunicorn 기동 시험은 gunicorn·prometheus_client가 설치된 Linux(CI)에서만 돈다.
"""
import http.server
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

from control_ui import accounts as accounts_module
from control_ui import app as ui
from control_ui import config as config_module
from control_ui import upstream
from control_ui.guard import CMD_LIMIT, IDLE_SEC, LOCK_SEC, MAX_AGE_SEC, Sessions
from control_ui.upstream import ControlApi, Unreachable, read_key

PASSWORD = 'Coldguard-2026!'
OTHER_PASSWORD = 'Avocado#Freezer9'
KEY = 'k' * 40
HASH = generate_password_hash(PASSWORD, method='scrypt')
ENV = {'CONTROL_API_URL': 'https://control-api.example.ts.net', 'STALE_SEC': '30',
       'GRAFANA_DEVICE_URL': 'http://hub.example.ts.net:3000/d/cg-dev?var-site={site}&var-device={device}'}
# README의 compose 예시와 같은 gunicorn 인자. 바꾸면 README도 같이 바꾼다.
GUNICORN_ARGS = ['--bind', '0.0.0.0:8081', '--workers', '1', '--worker-class', 'gthread', '--threads', '8',
                 '--worker-tmp-dir', '/tmp', '--graceful-timeout', '20', '--no-control-socket']
CMD = {'site_id': 'site01', 'device_id': 'fz01', 'value': -22.5}
CMD_VIEW = {'cmd_id': '11111111-1111-4111-8111-111111111111', 'site_id': 'site01', 'device_id': 'fz01',
            'action': 'set_temp', 'value': -22.5, 'issued_by': 'kt-ui:cloud-admin', 'status': 'pending'}


def write_private(path, text):
    Path(path).write_text(text, encoding='utf-8')
    os.chmod(path, 0o600)


def device(**over):
    d = dict(site_id='site01', device_id='fz01', seq=10, measured_at='2026-11-26T01:00:00.000Z',
             received_at='2026-11-26T01:00:00.200Z', age_sec=1.5, temp_c=-28.0, setpoint_c=-28.0,
             door_open=False, cooling=True, status='normal')
    d.update(over)
    return d


# ---------- 가짜 객체 ----------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, sec):
        self.now += sec


class FakeApi:
    """경로별 응답을 정해 두는 통제 API. 값이 예외면 던진다."""

    def __init__(self):
        self.calls = []
        self.routes = {
            ('GET', '/readyz'): (200, {'status': 'ready', 'db': True, 'mqtt': True}),
            ('GET', '/api/v1/me'): (200, {'name': 'kt-ui', 'scope': 'command', 'sites': ['site01']}),
            ('GET', '/api/v1/devices'): (200, {'devices': [device()]}),
            ('GET', '/api/v1/commands'): (200, {'commands': [CMD_VIEW]}),
            ('POST', '/api/v1/commands'): (202, CMD_VIEW),
        }

    def call(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body, auth))
        result = self.routes.get((method, path), self.routes.get((method, path.split('?')[0]),
                                                                  (404, {'error': 'not_found'})))
        if isinstance(result, Exception):
            raise result
        return result

    def posts(self):
        return [c for c in self.calls if c[0] == 'POST']


class RecordingSessions(Sessions):
    """앱 안에서 만든 세션 저장소를 시험에서 볼 수 있게 남긴다."""
    last = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        RecordingSessions.last = self


class FakeMetrics:
    def __init__(self):
        self.logins, self.commands = [], []

    def login(self, result):
        self.logins.append(result)

    def command(self, result):
        self.commands.append(result)


class UiCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.accounts_file = self.dir / 'accounts.json'
        write_private(self.accounts_file, json.dumps({
            'cloud-admin': HASH, 'monitor': generate_password_hash(OTHER_PASSWORD, method='scrypt')}))
        self.logs = []
        record = lambda event, **fields: self.logs.append(dict(event=event, **fields))  # noqa: E731
        for module in (ui, accounts_module):
            p = patch.object(module, 'log', record)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(ui, 'Sessions', RecordingSessions)
        p.start()
        self.addCleanup(p.stop)
        self.api, self.metrics, self.clock = FakeApi(), FakeMetrics(), Clock()
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, **env):
        self.cfg = config_module.load({**ENV, 'ACCOUNTS_FILE': str(self.accounts_file),
                                       'API_KEY_FILE': str(self.dir / 'unused'), **env})
        app = ui.create_app(cfg=self.cfg, api=self.api, metrics=self.metrics, clock=self.clock)
        self.sessions = RecordingSessions.last
        return app

    def login(self, user='cloud-admin', password=PASSWORD, client=None, **kw):
        return (client or self.client).post('/api/login', json={'username': user, 'password': password}, **kw)

    def signed_in(self, user='cloud-admin', password=PASSWORD, client=None):
        response = self.login(user, password, client)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['csrf']

    def post_command(self, csrf, body=None, client=None, **headers):
        return (client or self.client).post('/api/commands', json=CMD if body is None else body,
                                            headers={'X-CSRF-Token': csrf, **headers})

    def events(self, name):
        return [e for e in self.logs if e['event'] == name]


# ---------- 설정 ----------

class ConfigTests(unittest.TestCase):
    def test_required_values(self):
        with self.assertRaises(ValueError) as ctx:
            config_module.load({})
        self.assertIn('CONTROL_API_URL', str(ctx.exception))
        self.assertIn('STALE_SEC', str(ctx.exception))
        cfg = config_module.load({**ENV, 'CONTROL_API_URL': 'https://control-api.example.ts.net/'})
        self.assertEqual(cfg['CONTROL_API_URL'], 'https://control-api.example.ts.net')
        self.assertEqual((cfg['STALE_SEC'], cfg['METRICS_PORT'], cfg['METRICS_HOST']), (30.0, 9205, '0.0.0.0'))
        self.assertEqual(cfg['ACCOUNTS_FILE'], '/etc/control-ui/accounts.json')
        self.assertEqual(cfg['API_KEY_FILE'], '/etc/control-ui/api-key')

    def test_api_url_must_be_plain_https(self):
        for url in ('http://control-api.example.ts.net', 'https://control-api.example.ts.net/api',
                    'https://user:pw@control-api.example.ts.net', 'https://control-api.example.ts.net:abc',
                    'https://control-api.example.ts.net?x=1', 'control-api.example.ts.net'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config_module.load({**ENV, 'CONTROL_API_URL': url})

    def test_stale_sec_must_be_positive(self):
        for value in ('0', '-1', 'nan', 'inf', 'abc', ' '):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config_module.load({**ENV, 'STALE_SEC': value})

    def test_grafana_template(self):
        self.assertEqual(config_module.load({**ENV, 'GRAFANA_DEVICE_URL': ''})['GRAFANA_DEVICE_URL'], '')
        for url in ('http://hub:3000/d/cg-dev?var-site={site}', 'javascript:alert(1)//{site}{device}',
                    'hub:3000/{site}/{device}'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config_module.load({**ENV, 'GRAFANA_DEVICE_URL': url})


# ---------- 계정 파일·도구 ----------

class AccountTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = str(Path(tmp.name) / 'accounts.json')

    def tool(self, *args, answers=()):
        answers = list(answers)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = accounts_module.main(['--file', self.file, *args], prompt=lambda _: answers.pop(0), out=out)
        return code, out.getvalue(), err.getvalue()

    def test_password_rules(self):
        self.assertEqual(accounts_module.password_problems(PASSWORD, 'cloud-admin'), [])
        cases = {'Short-1a': '12자 이상', 'coldguard-2026!': '영문 대문자 포함', 'COLDGUARD-2026!': '영문 소문자 포함',
                 'Coldguard-Avocado!': '숫자 포함', 'Coldguard20261': '기호 포함',
                 'Cloud-Admin-2026!': '계정 ID 미포함', 'Coldguard-2026!\n': '제어 문자 없음'}
        for password, problem in cases.items():
            with self.subTest(password=password):
                self.assertIn(problem, accounts_module.password_problems(password, 'cloud-admin'))

    def test_parse_rejects_bad_files(self):
        for text in ('[]', '{"cloud-admin": "%s", "cloud-admin": "%s"}' % (HASH, HASH), '{"Young Bin": "%s"}' % HASH,
                     '{"cloud-admin": "plain-password"}', '{"cloud-admin": 1}', 'not json'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                accounts_module.parse(text)

    def test_tool_add_passwd_remove_list(self):
        self.assertEqual(self.tool('add', 'cloud-admin', answers=[PASSWORD, PASSWORD])[0], 0)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        saved = accounts_module.load(self.file)
        self.assertTrue(check_password_hash(saved['cloud-admin'], PASSWORD))
        self.assertNotIn(PASSWORD, Path(self.file).read_text(encoding='utf-8'))
        self.assertEqual(self.tool('add', 'monitor', answers=[OTHER_PASSWORD, OTHER_PASSWORD])[0], 0)
        self.assertEqual(self.tool('list')[1].split(), ['cloud-admin', 'monitor'])
        self.assertEqual(self.tool('passwd', 'cloud-admin', answers=[OTHER_PASSWORD, OTHER_PASSWORD])[0], 0)
        self.assertTrue(check_password_hash(accounts_module.load(self.file)['cloud-admin'], OTHER_PASSWORD))
        self.assertEqual(self.tool('remove', 'monitor')[0], 0)
        self.assertEqual(sorted(accounts_module.load(self.file)), ['cloud-admin'])

    def test_tool_refuses_and_leaves_file_unchanged(self):
        self.assertEqual(self.tool('passwd', 'cloud-admin', answers=[PASSWORD, PASSWORD])[0], 1)  # 파일 없음
        self.assertFalse(os.path.exists(self.file))
        self.tool('add', 'cloud-admin', answers=[PASSWORD, PASSWORD])
        before = Path(self.file).read_bytes()
        for args, answers in ((('add', 'cloud-admin'), [PASSWORD, PASSWORD]),       # 이미 있음
                              (('passwd', 'nobody'), [PASSWORD, PASSWORD]),      # 없는 계정
                              (('add', 'monitor'), ['weak', 'weak']),             # 규칙 위반
                              (('add', 'monitor'), [PASSWORD, OTHER_PASSWORD]),   # 두 번 다름
                              (('add', 'Bad User'), [PASSWORD, PASSWORD])):       # ID 형식
            with self.subTest(args=args):
                code, _, err = self.tool(*args, answers=answers)
                self.assertEqual(code, 1)
                self.assertIn('오류', err)
                self.assertNotIn(PASSWORD, err)
        self.assertEqual(Path(self.file).read_bytes(), before)

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0, 'root에서만')
    def test_save_keeps_owner_for_container_user(self):
        self.tool('add', 'cloud-admin', answers=[PASSWORD, PASSWORD])
        os.chown(self.file, 10001, 10001)
        self.tool('passwd', 'cloud-admin', answers=[OTHER_PASSWORD, OTHER_PASSWORD])
        st = os.stat(self.file)
        self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)), (10001, 10001, 0o600))

    @unittest.skipUnless(os.name == 'posix', 'POSIX 권한')
    def test_server_refuses_readable_files(self):
        Path(self.file).write_text(json.dumps({'cloud-admin': HASH}), encoding='utf-8')
        os.chmod(self.file, 0o644)
        with self.assertRaises(ValueError):
            accounts_module.AccountBook(self.file)
        os.chmod(self.file, 0o600)
        self.assertEqual(accounts_module.AccountBook(self.file).users(), ['cloud-admin'])
        write_private(self.file, '{}')
        with self.assertRaises(ValueError):  # 계정이 하나도 없으면 기동하지 않는다
            accounts_module.AccountBook(self.file)


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / 'api-key'

    def test_strips_line_endings_and_bom(self):
        for text in (KEY + '\r\n', KEY + '\n', ' ' + KEY + ' ', '﻿' + KEY):
            with self.subTest(text=repr(text[-3:])):
                write_private(self.file, text)
                self.assertEqual(read_key(str(self.file)), KEY)

    def test_rejects_bad_keys(self):
        for text in ('short', 'k' * 20 + ' ' + 'k' * 20, 'k' * 39 + 'é', ''):
            with self.subTest(text=text[:10]), self.assertRaises(ValueError):
                write_private(self.file, text)
                read_key(str(self.file))
        if os.name == 'posix':
            write_private(self.file, KEY)
            os.chmod(self.file, 0o640)
            with self.assertRaises(ValueError):
                read_key(str(self.file))


# ---------- 로그인 ----------

class LoginTests(UiCase):
    def test_login_sets_strict_session_cookie(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['Set-Cookie']
        for part in ('__Host-cg-ui=', 'Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(part, cookie)
        self.assertNotIn('Domain=', cookie)
        body = response.json
        self.assertEqual((body['user'], body['stale_sec'], body['idle_sec'], body['max_age_sec'], body['cmd_limit']),
                         ('cloud-admin', 30.0, IDLE_SEC, MAX_AGE_SEC, CMD_LIMIT))
        self.assertGreaterEqual(len(body['csrf']), 32)
        self.assertEqual(self.client.get('/api/session').json['user'], 'cloud-admin')
        self.assertEqual(self.metrics.logins, ['success'])
        self.assertEqual(self.events('ui_login'), [dict(event='ui_login', user='cloud-admin', result='success',
                                                        remote='127.0.0.1')])

    def test_five_failures_lock_for_five_minutes(self):
        for _ in range(4):
            response = self.login(password='Wrong-password1')
            self.assertEqual((response.status_code, response.json), (401, {'error': 'invalid_credentials'}))
        response = self.login(password='Wrong-password1')
        self.assertEqual((response.status_code, response.json['error'], response.json['retry_after']),
                         (429, 'locked', LOCK_SEC))
        self.assertEqual(response.headers['Retry-After'], str(LOCK_SEC))
        self.clock.advance(LOCK_SEC - 1)
        response = self.login()  # 맞는 비밀번호도 잠금 동안은 거부
        self.assertEqual((response.status_code, response.json['retry_after']), (429, 1))
        self.clock.advance(1)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.metrics.logins, ['fail'] * 5 + ['locked', 'success'])
        self.assertEqual([e['result'] for e in self.events('ui_login')], ['fail'] * 5 + ['locked', 'success'])
        self.assertTrue(self.events('ui_login')[4]['locked'])

    def test_success_resets_failure_count(self):
        for _ in range(4):
            self.login(password='Wrong-password1')
        self.assertEqual(self.login().status_code, 200)
        for _ in range(4):
            self.assertEqual(self.login(password='Wrong-password1').status_code, 401)

    def test_unknown_user_gets_the_same_answers(self):
        # 없는 계정도 같은 응답·같은 잠금을 받아서 계정이 있는지 알 수 없다.
        answers = [self.login('nobody1', 'Wrong-password1').status_code for _ in range(5)]
        self.assertEqual(answers, [401] * 4 + [429])
        self.assertEqual(self.login('cloud-admin').status_code, 200)  # 다른 계정은 영향 없음

    def test_malformed_user_id_is_not_logged(self):
        response = self.login(PASSWORD, PASSWORD)  # 비밀번호를 아이디 칸에 넣은 경우
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.events('ui_login')[0]['user'])
        self.assertNotIn(PASSWORD, json.dumps(self.logs, ensure_ascii=False))

    def test_login_request_shape(self):
        self.assertEqual(self.client.post('/api/login', data='username=cloud-admin').status_code, 415)
        for body in ({'username': 'cloud-admin'}, {'username': 'cloud-admin', 'password': PASSWORD, 'x': 1},
                     {'username': 'cloud-admin', 'password': 1}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/login', json=body).status_code, 400)
        response = self.login(headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual((response.status_code, response.json['error']), (403, 'cross_site'))
        self.assertEqual(self.metrics.logins, [])

    def test_each_login_gets_new_session(self):
        first = self.login().headers['Set-Cookie']
        second = self.login().headers['Set-Cookie']
        self.assertNotEqual(first.split(';')[0], second.split(';')[0])
        other = self.app.test_client()
        other.set_cookie(ui.COOKIE, first.split(';')[0].split('=', 1)[1])
        self.assertEqual(other.get('/api/session').status_code, 401)  # 이전 세션은 끝났다


# ---------- 세션 ----------

class SessionTests(UiCase):
    def test_login_required(self):
        for path in ('/api/session', '/api/status', '/api/devices', '/api/commands', '/api/commands/abc'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual((response.status_code, response.json['error']), (401, 'unauthorized'))
        self.assertEqual(self.client.post('/api/commands', json=CMD).status_code, 401)
        self.assertEqual(self.api.calls, [])

    def test_background_refresh_does_not_extend_idle_time(self):
        self.signed_in()
        for _ in range(IDLE_SEC // 900 - 1):
            self.clock.advance(900)
            self.assertEqual(self.client.get('/api/devices').status_code, 200)
        self.clock.advance(900)
        for _ in range(3):  # 화면이 함께 보낸 요청이 모두 같은 이유를 받는다(로그는 한 번)
            response = self.client.get('/api/devices')
            self.assertEqual((response.status_code, response.json['error']), (401, 'session_expired'))
        self.assertEqual(self.events('ui_logout'), [dict(event='ui_logout', user='cloud-admin', reason='idle')])
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.sessions.count(), 1)  # 끝난 세션은 다음 로그인 때 정리된다

    def test_user_activity_extends_idle_time(self):
        self.signed_in()
        self.clock.advance(IDLE_SEC - 60)
        self.assertEqual(self.client.get('/api/devices', headers={'X-User-Active': '1'}).status_code, 200)
        self.clock.advance(IDLE_SEC - 60)
        self.assertEqual(self.client.get('/api/devices').status_code, 200)

    def test_session_ends_after_one_day(self):
        self.signed_in()
        for _ in range(MAX_AGE_SEC // 3600 - 1):
            self.clock.advance(3600)
            self.assertEqual(self.client.get('/api/devices', headers={'X-User-Active': '1'}).status_code, 200)
        self.clock.advance(3600)
        response = self.client.get('/api/devices', headers={'X-User-Active': '1'})
        self.assertEqual((response.status_code, response.json['error']), (401, 'session_expired'))
        self.assertEqual(self.events('ui_logout')[0]['reason'], 'max_age')

    def test_logout(self):
        csrf = self.signed_in()
        self.assertEqual(self.client.post('/api/logout').status_code, 403)  # CSRF 토큰 없음
        response = self.client.post('/api/logout', headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIn('__Host-cg-ui=;', response.headers['Set-Cookie'])
        self.assertEqual(self.client.get('/api/session').status_code, 401)
        self.assertEqual(self.events('ui_logout'), [dict(event='ui_logout', user='cloud-admin', reason='user')])

    def test_password_change_or_removal_ends_sessions(self):
        self.signed_in()
        other = self.app.test_client()
        self.signed_in('monitor', OTHER_PASSWORD, other)
        accounts = accounts_module.load(str(self.accounts_file))
        accounts['cloud-admin'] = generate_password_hash(OTHER_PASSWORD, method='scrypt')
        del accounts['monitor']
        accounts_module.save(str(self.accounts_file), accounts)
        self.assertEqual(self.client.get('/api/devices').status_code, 401)
        self.assertEqual(other.get('/api/devices').status_code, 401)
        self.assertEqual(sorted(e['user'] for e in self.events('ui_logout')), ['cloud-admin', 'monitor'])
        self.assertEqual(self.login().status_code, 401)  # 예전 비밀번호
        self.assertEqual(self.login(password=OTHER_PASSWORD).status_code, 200)

    def test_broken_accounts_file_keeps_last_good_list(self):
        csrf = self.signed_in()
        write_private(self.accounts_file, '{broken')
        for _ in range(3):
            self.assertEqual(self.client.get('/api/devices').status_code, 200)
        self.assertEqual(len(self.events('control_ui_accounts_error')), 1)  # 같은 오류는 한 번만 기록
        self.assertEqual(self.post_command(csrf).status_code, 202)
        self.assertEqual(self.login('monitor', OTHER_PASSWORD, self.app.test_client()).status_code, 200)


# ---------- CSRF·다른 사이트 요청 ----------

class CsrfTests(UiCase):
    def test_post_needs_matching_token(self):
        csrf = self.signed_in()
        for token in ('', 'x' * 43, csrf[:-1]):
            with self.subTest(token=token):
                response = self.post_command(token)
                self.assertEqual((response.status_code, response.json['error']), (403, 'csrf_failed'))
        self.assertEqual(self.api.posts(), [])
        self.assertEqual(self.metrics.commands, [])  # 명령 처리 전에 막힌다
        self.assertEqual(len(self.events('ui_request_rejected')), 3)

    def test_cross_site_request_rejected(self):
        csrf = self.signed_in()
        for site in ('cross-site', 'same-site', 'none'):
            with self.subTest(site=site):
                response = self.post_command(csrf, **{'Sec-Fetch-Site': site})
                self.assertEqual((response.status_code, response.json['error']), (403, 'cross_site'))
        self.assertEqual(self.post_command(csrf, **{'Sec-Fetch-Site': 'same-origin'}).status_code, 202)
        self.assertEqual(len(self.api.posts()), 1)


# ---------- 명령 ----------

class CommandTests(UiCase):
    def test_operator_is_the_login_account(self):
        csrf = self.signed_in()
        response = self.post_command(csrf)
        self.assertEqual((response.status_code, response.json), (202, CMD_VIEW))
        self.assertEqual(self.api.posts(), [('POST', '/api/v1/commands', {**CMD, 'operator': 'cloud-admin'}, True)])
        self.assertEqual(self.metrics.commands, ['sent'])
        self.assertEqual(self.events('ui_command'), [dict(
            event='ui_command', user='cloud-admin', remote='127.0.0.1', site_id='site01', device_id='fz01',
            value=-22.5, result='sent', cmd_id=CMD_VIEW['cmd_id'])])

    def test_client_cannot_choose_operator(self):
        csrf = self.signed_in()
        response = self.post_command(csrf, {**CMD, 'operator': 'someone-else'})
        self.assertEqual((response.status_code, response.json), (400, {'error': 'unknown_field',
                                                                        'fields': ['operator']}))
        self.assertEqual(self.api.posts(), [])
        self.assertEqual(self.metrics.commands, ['invalid_request'])

    def test_input_checks_before_forwarding(self):
        csrf = self.signed_in()
        cases = [({'site_id': 'site01', 'device_id': 'fz01'}, 'missing_field'),
                 ({**CMD, 'device_id': 'fz-1'}, 'invalid_device_id'),
                 ({**CMD, 'site_id': 'other'}, 'invalid_site_id'),
                 ({**CMD, 'value': '-22'}, 'invalid_value'),
                 ({**CMD, 'value': True}, 'invalid_value')]
        for body, error in cases:
            with self.subTest(body=body):
                response = self.post_command(csrf, body)
                self.assertEqual((response.status_code, response.json['error']), (400, error))
        response = self.client.post('/api/commands', data='value=-22', headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.api.posts(), [])
        self.assertEqual(self.metrics.commands, ['invalid_request'] * 6)

    def test_ten_commands_per_minute_per_user(self):
        csrf = self.signed_in()
        for _ in range(CMD_LIMIT):
            self.assertEqual(self.post_command(csrf).status_code, 202)
        self.clock.advance(30)
        response = self.post_command(csrf)
        self.assertEqual((response.status_code, response.json), (429, {'error': 'rate_limited', 'retry_after': 30}))
        self.assertEqual(response.headers['Retry-After'], '30')
        self.assertEqual(len(self.api.posts()), CMD_LIMIT)  # 막힌 요청은 통제 API로 가지 않는다
        other = self.app.test_client()
        other_csrf = self.signed_in('monitor', OTHER_PASSWORD, other)
        self.assertEqual(self.post_command(other_csrf, client=other).status_code, 202)  # 사용자별로 센다
        self.clock.advance(30)
        self.assertEqual(self.post_command(csrf).status_code, 202)
        self.assertEqual(self.metrics.commands.count('rate_limited'), 1)

    def test_api_answers_are_relayed_without_retry(self):
        csrf = self.signed_in()
        cases = [
            ((503, {'error': 'mqtt_unavailable', 'sent': False}), 503, {'error': 'mqtt_unavailable', 'sent': False},
             'mqtt_unavailable'),
            ((503, {'error': 'db_unavailable'}), 503, {'error': 'db_unavailable'}, 'db_unavailable'),
            ((503, {'error': 'publish_unconfirmed', 'cmd_id': 'abc', 'detail': '결과 조회로 확인'}), 503,
             {'error': 'publish_unconfirmed', 'detail': '결과 조회로 확인', 'cmd_id': 'abc'}, 'publish_unconfirmed'),
            ((400, {'error': 'invalid_value', 'detail': '소수 둘째 자리까지'}), 400,
             {'error': 'invalid_value', 'detail': '소수 둘째 자리까지'}, 'invalid_request'),
            ((401, {'error': 'unauthorized'}), 502, {'error': 'upstream_auth'}, 'upstream_auth'),
            ((403, {'error': 'forbidden'}), 502, {'error': 'upstream_auth'}, 'upstream_auth'),
            ((500, {'error': 'internal_error'}), 502, {'error': 'api_error'}, 'api_error'),
            ((202, None), 502, {'error': 'api_error'}, 'api_error'),
            (Unreachable(False, 'ConnectionRefusedError'), 502, {'error': 'api_unreachable', 'sent': False},
             'api_unreachable'),
            (Unreachable(True, 'TimeoutError'), 504, {'error': 'api_no_response'}, 'api_no_response'),
        ]
        for answer, status, body, result in cases:
            with self.subTest(result=result, answer=answer):
                self.api.routes[('POST', '/api/v1/commands')] = answer
                before = len(self.api.posts())
                response = self.post_command(csrf)
                self.assertEqual((response.status_code, response.json), (status, body))
                self.assertEqual(len(self.api.posts()), before + 1)  # 자동 재전송 없음
                self.assertEqual(self.metrics.commands[-1], result)
                self.assertEqual(self.events('ui_command')[-1]['result'], result)
                self.clock.advance(CMD_LIMIT)  # 명령 제한에 걸리지 않게
        self.assertEqual(self.events('ui_command')[2]['cmd_id'], 'abc')


# ---------- 조회 ----------

class ReadTests(UiCase):
    def view(self, **over):
        self.api.routes[('GET', '/api/v1/devices')] = (200, {'devices': [device(**over)]})
        response = self.client.get('/api/devices')
        self.assertEqual(response.status_code, 200)
        return response.json['devices'][0]

    def test_device_labels_follow_field_web(self):
        self.signed_in()
        normal = ('normal', '정상')
        cases = [
            (dict(temp_c=-28.0), normal + ('정상적으로 운영 중입니다.',)),
            (dict(temp_c=-27.0), normal + ('정상적으로 운영 중입니다.',)),
            (dict(temp_c=-26.5), normal + ('정상 운전',)),
            (dict(temp_c=-20.0, status='high_temp'), ('abnormal', '비정상', '고온 경보 - 위험 온도 도달')),
            (dict(age_sec=30.0), normal + ('정상적으로 운영 중입니다.',)),
            (dict(age_sec=30.5), ('unknown', '확인 불가', '데이터 확인 불가')),
            (dict(age_sec=31.0, status='high_temp'), ('unknown', '확인 불가', '데이터 확인 불가')),
            (dict(age_sec=-0.4), normal + ('정상적으로 운영 중입니다.',)),
            (dict(age_sec=-31.0), ('unknown', '확인 불가', '데이터 확인 불가')),
            (dict(status='weird'), ('unknown', '확인 불가', '데이터 확인 불가')),
            (dict(temp_c=None), ('unknown', '확인 불가', '데이터 확인 불가')),
        ]
        for over, expected in cases:
            with self.subTest(over=over):
                d = self.view(**over)
                self.assertEqual((d['state'], d['state_label'], d['state_detail']), expected)
        d = self.view()
        self.assertEqual(d['grafana_url'], 'http://hub.example.ts.net:3000/d/cg-dev?var-site=site01&var-device=fz01')
        self.assertEqual((d['door_open'], d['cooling'], d['setpoint_c']), (False, True, -28.0))
        self.assertIsNone(self.view(device_id='../x')['grafana_url'])

    def test_no_grafana_link_without_template(self):
        self.client = self.make_app(GRAFANA_DEVICE_URL='').test_client()
        self.signed_in()
        self.assertIsNone(self.view()['grafana_url'])

    def test_status(self):
        self.signed_in()
        ready = ('GET', '/readyz')
        me = ('GET', '/api/v1/me')
        cases = [
            ({}, dict(api=True, db=True, mqtt=True, key=True, can_command=True)),
            ({ready: (200, {'status': 'ready', 'db': True, 'mqtt': False})},
             dict(api=True, db=True, mqtt=False, key=True, can_command=False)),
            ({ready: (503, {'status': 'not_ready', 'db': False, 'mqtt': True})},
             dict(api=True, db=False, mqtt=True, key=True, can_command=False)),
            ({me: (401, {'error': 'unauthorized'})}, dict(api=True, db=True, mqtt=True, key=False, can_command=False)),
            ({me: (200, {'name': 'kt-load', 'scope': 'read'})},
             dict(api=True, db=True, mqtt=True, key=False, can_command=False)),
            ({me: (500, {'error': 'internal_error'})}, dict(api=True, db=True, mqtt=True, key=None, can_command=False)),
            ({ready: Unreachable(False, 'gaierror')}, dict(api=False, db=False, mqtt=False, key=None, can_command=False)),
            ({ready: (502, None)}, dict(api=False, db=False, mqtt=False, key=None, can_command=False)),
        ]
        base = dict(self.api.routes)
        for routes, expected in cases:
            with self.subTest(routes=routes):
                self.api.routes = {**base, **routes}
                self.assertEqual(self.client.get('/api/status').json, expected)
        readyz = [c for c in self.api.calls if c[1] == '/readyz']
        self.assertTrue(all(c[3] is False for c in readyz))  # /readyz에는 키를 보내지 않는다

    def test_command_history_and_lookup(self):
        self.signed_in()
        self.assertEqual(self.client.get('/api/commands').json, {'commands': [CMD_VIEW]})
        self.client.get('/api/commands?device_id=fz02')
        self.assertEqual([c[1] for c in self.api.calls], ['/api/v1/commands?limit=50',
                                                           '/api/v1/commands?limit=50&device_id=fz02'])
        self.assertEqual(self.client.get('/api/commands?device_id=fz02%26x').status_code, 400)
        self.api.routes[('GET', '/api/v1/commands/abc-1')] = (200, CMD_VIEW)
        self.assertEqual(self.client.get('/api/commands/abc-1').json, CMD_VIEW)
        self.assertEqual(self.client.get('/api/commands/' + 'a' * 51).status_code, 404)
        self.assertEqual(self.client.get('/api/commands/zzz').status_code, 404)  # 통제 API의 404
        self.assertEqual(len(self.api.calls), 4)

    def test_api_key_problem_does_not_log_the_user_out(self):
        self.signed_in()
        self.api.routes[('GET', '/api/v1/devices')] = (401, {'error': 'unauthorized'})
        response = self.client.get('/api/devices')
        self.assertEqual((response.status_code, response.json), (502, {'error': 'upstream_auth'}))
        self.assertEqual(self.client.get('/api/session').status_code, 200)
        self.assertEqual(self.events('control_ui_api_rejected')[0]['status'], 401)
        self.api.routes[('GET', '/api/v1/devices')] = Unreachable(False, 'ConnectionRefusedError')
        self.assertEqual(self.client.get('/api/devices').json, {'error': 'api_unreachable'})


# ---------- 화면·보안 헤더 ----------

class PageTests(UiCase):
    def test_security_headers(self):
        for path, cache in (('/', 'no-store'), ('/static/app.js', 'no-cache'), ('/static/app.css', 'no-cache'),
                            ('/api/session', 'no-store'), ('/healthz', 'no-store'), ('/nothing', 'no-store')):
            with self.subTest(path=path), self.client.get(path) as response:
                h = response.headers
                self.assertEqual(h['Content-Security-Policy'], ui.CSP)
                self.assertEqual((h['X-Frame-Options'], h['Referrer-Policy'], h['X-Content-Type-Options']),
                                 ('DENY', 'no-referrer', 'nosniff'))
                self.assertEqual(h['Cache-Control'], cache)
                self.assertNotIn('Strict-Transport-Security', h)  # 같은 호스트의 http Grafana를 막지 않게
        self.assertEqual(self.client.get('/healthz').json, {'status': 'ok'})

    def test_page_has_no_inline_script_and_no_key_handling(self):
        root = Path(ui.__file__).parent / 'static'
        html = (root / 'index.html').read_text(encoding='utf-8')
        self.assertNotRegex(html, r'<script(?![^>]*\bsrc=)')  # CSP script-src 'self'과 맞게 인라인 스크립트 없음
        script = (root / 'app.js').read_text(encoding='utf-8')
        for word in ('Authorization', 'localStorage', 'sessionStorage', 'innerHTML', 'operator'):
            self.assertNotIn(word, script)

    def test_api_key_and_password_never_leave_the_server(self):
        real = ui.create_app(cfg=self.cfg, api=ControlApi('https://control-api.example.ts.net', KEY,
                                                          opener=_FakeOpener()),
                             metrics=self.metrics, clock=self.clock)
        client = real.test_client()
        seen = []
        csrf = self.signed_in(client=client)
        for method, path, kw in (('GET', '/', {}), ('GET', '/static/app.js', {}), ('GET', '/api/session', {}),
                                 ('GET', '/api/status', {}), ('GET', '/api/devices', {}), ('GET', '/api/commands', {}),
                                 ('POST', '/api/commands', dict(json=CMD, headers={'X-CSRF-Token': csrf})),
                                 ('POST', '/api/logout', dict(headers={'X-CSRF-Token': csrf}))):
            with client.open(path, method=method, **kw) as response:
                seen.append(response.get_data(as_text=True) + str(response.headers))
        self.assertNotIn(KEY, '\n'.join(seen))
        self.assertNotIn(KEY, json.dumps(self.logs))
        self.assertNotIn(PASSWORD, json.dumps(self.logs, ensure_ascii=False))
        self.assertTrue(_FakeOpener.headers)
        self.assertTrue(all(h.get('Authorization') == 'Bearer ' + KEY for p, h in _FakeOpener.headers
                            if p != '/readyz'))


class _FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


class _FakeOpener:
    """ControlApi가 실제로 붙이는 헤더를 보려고 urllib opener만 바꾼다."""
    headers = []

    def open(self, request, timeout):
        path = request.full_url.split('ts.net', 1)[1]
        _FakeOpener.headers.append((path.split('?')[0], dict(request.header_items())))
        answers = FakeApi().routes
        status, body = answers.get((request.get_method(), path.split('?')[0]), (404, {'error': 'not_found'}))
        return _FakeResponse(status, body)


# ---------- 통제 API 호출(실제 HTTP) ----------

class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=()):
        try:
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 시간 초과 시험에서 클라이언트가 먼저 끊는다

    def do_GET(self):
        _Handler.seen.append((self.path, self.headers.get('Authorization')))
        if self.path == '/ok':
            self.reply(200, b'{"ok": true}')
        elif self.path == '/redirect':
            self.reply(302, b'', (('Location', '/stolen'),))
        elif self.path == '/slow':
            time.sleep(1.5)
            self.reply(200, b'{}')
        elif self.path == '/big':
            self.reply(200, b'{"x": "' + b'a' * 200 + b'"}')
        elif self.path == '/text':
            self.reply(200, b'hello')
        else:
            self.reply(404, b'{"error": "not_found"}')

    do_POST = do_GET


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.seen.clear()
        # ControlApi 기본 opener는 https만 다루지만 http 처리기도 들어 있다. 시험은 로컬 http 서버로 한다.
        self.api = ControlApi(self.base, KEY)

    def test_sends_key_and_reads_json(self):
        self.assertEqual(self.api.call('GET', '/ok'), (200, {'ok': True}))
        self.assertEqual(self.api.call('GET', '/missing'), (404, {'error': 'not_found'}))
        self.assertEqual(self.api.call('GET', '/text'), (200, None))
        self.assertEqual(_Handler.seen[0], ('/ok', 'Bearer ' + KEY))
        self.api.call('GET', '/ok', auth=False)
        self.assertIsNone(_Handler.seen[-1][1])

    def test_does_not_follow_redirects(self):
        self.assertEqual(self.api.call('GET', '/redirect'), (302, None))
        self.assertEqual([p for p, _ in _Handler.seen], ['/redirect'])  # 키를 다른 경로로 보내지 않는다

    def test_ignores_proxy_environment(self):
        with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:9', 'HTTP_PROXY': 'http://127.0.0.1:9',
                                     'no_proxy': '', 'NO_PROXY': ''}):
            self.assertEqual(ControlApi(self.base, KEY).call('GET', '/ok'), (200, {'ok': True}))

    def test_response_size_limit(self):
        with patch.object(upstream, 'MAX_BYTES', 100):
            self.assertEqual(self.api.call('GET', '/big'), (200, None))

    def test_unreachable_tells_whether_the_request_was_sent(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            closed = s.getsockname()[1]
        with self.assertRaises(Unreachable) as ctx:
            ControlApi(f'http://127.0.0.1:{closed}', KEY).call('POST', '/x', {'a': 1})
        self.assertFalse(ctx.exception.sent)  # 연결 실패: 명령이 나가지 않았다
        with patch.object(upstream, 'POST_TIMEOUT', 0.5), self.assertRaises(Unreachable) as ctx:
            self.api.call('POST', '/slow', {'a': 1})
        self.assertTrue(ctx.exception.sent)  # 보낸 뒤 응답 없음: 처리됐는지 알 수 없다


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


def _http(port, method, path, body=b'', headers=()):
    lines = [f'{method} {path} HTTP/1.1', 'Host: coldguard-kt-hub', 'Connection: close', *headers]
    if body:
        lines.append(f'Content-Length: {len(body)}')
    with socket.create_connection(('127.0.0.1', port), timeout=10) as s:
        s.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode() + body)
        received = b''
        while chunk := s.recv(65536):
            received += chunk
    head, _, data = received.partition(b'\r\n\r\n')
    return int(head.split(b' ', 2)[1]), head.decode('latin-1'), data


@unittest.skipUnless(sys.platform.startswith('linux') and all(map(_installed, ('gunicorn', 'prometheus_client'))),
                     'gunicorn·prometheus_client가 설치된 Linux(CI)에서만 실행')
class GunicornTests(unittest.TestCase):
    """README compose 예시와 같은 gunicorn 인자로 실제 기동한다. 통제 API는 닫힌 포트를 가리킨다."""

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        write_private(self.tmp / 'api-key', KEY + '\r\n')
        write_private(self.tmp / 'accounts.json', json.dumps({'cloud-admin': HASH}))
        self.metrics_port = _free_port()
        self.env = {**os.environ, **ENV, 'CONTROL_API_URL': f'https://127.0.0.1:{_free_port()}',
                    'API_KEY_FILE': str(self.tmp / 'api-key'), 'ACCOUNTS_FILE': str(self.tmp / 'accounts.json'),
                    'METRICS_HOST': '127.0.0.1', 'METRICS_PORT': str(self.metrics_port)}

    def test_check_config_loads_factory(self):
        result = subprocess.run([sys.executable, '-m', 'gunicorn', '--check-config', *GUNICORN_ARGS,
                                 'control_ui.app:create_app()'], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('control_ui_start', result.stdout)

    def test_serves_login_metrics_and_stops(self):
        port = _free_port()
        args = list(GUNICORN_ARGS)
        args[args.index('--bind') + 1] = f'127.0.0.1:{port}'
        out = open(self.tmp / 'out.log', 'w+')
        self.addCleanup(out.close)
        proc = subprocess.Popen([sys.executable, '-m', 'gunicorn', *args, 'control_ui.app:create_app()'],
                                cwd=self.root, env=self.env, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        def logs():
            out.seek(0)
            return out.read()

        deadline = time.monotonic() + 30
        while True:
            self.assertIsNone(proc.poll(), logs())
            try:
                if _http(port, 'GET', '/healthz')[0] == 200:
                    break
            except OSError:
                pass
            self.assertLess(time.monotonic(), deadline, logs())
            time.sleep(0.3)

        body = json.dumps({'username': 'cloud-admin', 'password': PASSWORD}).encode()
        status, head, data = _http(port, 'POST', '/api/login', body, ('Content-Type: application/json',
                                                                      'X-Forwarded-For: 100.64.0.7'))
        self.assertEqual(status, 200, logs())
        cookie = next(line.split(':', 1)[1].split(';')[0].strip() for line in head.split('\r\n')
                      if line.lower().startswith('set-cookie:'))
        status, _, data = _http(port, 'GET', '/api/status', headers=(f'Cookie: {cookie}',))
        self.assertEqual((status, json.loads(data)['api']), (200, False))  # 통제 API에 닿지 않음
        status, _, data = _http(self.metrics_port, 'GET', '/metrics')
        self.assertEqual(status, 200)
        self.assertIn(b'coldguard_ui_logins_total{result="success"} 1.0', data)
        self.assertIn(b'coldguard_ui_commands_total{result="sent"} 0.0', data)
        self.assertEqual(_http(port, 'GET', '/metrics')[0], 404)  # 화면 포트에는 지표가 없다

        proc.terminate()
        self.assertEqual(proc.wait(timeout=30), 0, logs())
        text = logs()
        self.assertIn('"remote": "100.64.0.7"', text)  # serve가 붙인 접속 기기 IP를 기록
        self.assertNotIn(KEY, text)
        self.assertNotIn('Traceback', text)


if __name__ == '__main__':
    unittest.main()
