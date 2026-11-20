"""외부 서버 없이 실행하는 설정 / 프로토콜 / 온도 판정 회귀 테스트."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from common import coldguard_config as config
from common import coldguard_protocol as protocol
from common.temperature_analysis import analyze
import main as launcher


class SettingsTests(unittest.TestCase):
    def test_environment_overrides_file(self):
        with patch.object(config, 'MQTT_HOST', 'file-host'):
            self.assertEqual(config.values('collector', {})['MQTT_HOST'], 'file-host')
            self.assertEqual(config.values('collector', {'MQTT_HOST': 'env-host'})['MQTT_HOST'], 'env-host')

    def test_common_password_overrides_role(self):
        env = {'MQTT_PASS': 'dummy-common', 'COLLECTOR_PASSWORD': 'dummy-role'}
        self.assertEqual(config.values('collector', env)['MQTT_PASS'], 'dummy-common')
        del env['MQTT_PASS']
        self.assertEqual(config.values('collector', env)['MQTT_PASS'], 'dummy-role')

    def test_empty_environment_is_not_fallback(self):
        self.assertEqual(config.values('collector', {'MQTT_HOST': ''})['MQTT_HOST'], '')

    def test_identity_and_state_paths(self):
        cfg = config.values('simulator', {'SITE': 'site03', 'DEVICE': 'fz02'})
        self.assertEqual(cfg['MQTT_USER'], 'fz02')
        self.assertEqual(cfg['MQTT_CLIENT_ID'], 'sim-site03-fz02')
        self.assertTrue(cfg['SIM_DB'].endswith('site03-fz02-v1.db'))

    def test_boolean_settings(self):
        for value in ('true', '1', 'YES', 'on'):
            with self.subTest(value=value):
                self.assertEqual(config.values('simulator', {'SIM_MQTT': value})['SIM_MQTT'], '1')
        with self.assertRaises(ValueError):
            config.values('simulator', {'SIM_MQTT': 'maybe'})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        # 고정 시각을 사용해 실행 날짜에 따라 테스트 결과가 바뀌지 않게 한다.
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.cmd = dict(site_id='site01', device_id='fz01', cmd_id='test-1',
                        action='set_temp', value=-30, issued_by='test',
                        issued_at=protocol.utc(self.now),
                        expires_at=protocol.utc(self.now + timedelta(seconds=300)))
        self.source = 'coldguard/site01/fz01/cmd'

    def reason(self, **kwargs):
        return protocol.command_reason(self.cmd, self.source, 'site01', ['fz01'], now=self.now, **kwargs)

    def test_ttl_boundary(self):
        self.assertIsNone(self.reason())
        self.cmd['expires_at'] = protocol.utc(self.now + timedelta(seconds=301))
        self.assertEqual(self.reason(), 'expired')

    def test_future_issue_has_no_five_second_limit(self):
        self.cmd['issued_at'] = protocol.utc(self.now + timedelta(seconds=30))
        self.cmd['expires_at'] = protocol.utc(self.now + timedelta(seconds=330))
        self.assertIsNone(self.reason())

    def test_expiry_boundary(self):
        self.cmd['expires_at'] = protocol.utc(self.now)
        self.assertEqual(self.reason(), 'expired')

    def test_decision_priority(self):
        self.cmd['value'] = 0
        self.assertEqual(self.reason(locked=True), 'locked')
        self.assertEqual(self.reason(duplicate=True, locked=True), 'duplicate')
        self.cmd['device_id'] = 'fz02'
        self.assertEqual(self.reason(duplicate=True), 'unknown_device')

    def test_range_and_rounding(self):
        for value, expected in ((-50, None), (-10, 'out_of_range'), (-50.1, 'out_of_range'), (-10.001, 'out_of_range')):
            with self.subTest(value=value):
                self.cmd['value'] = value
                self.assertEqual(self.reason(), expected)
        self.assertEqual(protocol.temperature(-20.125), -20.13)

    def test_apply_preserves_expiry_and_rounds(self):
        self.cmd['value'] = -20.123
        data = protocol.approved(self.cmd, self.now)
        self.assertEqual(data['expires_at'], self.cmd['expires_at'])
        self.assertEqual(protocol.check_apply(data, self.source.replace('/cmd', '/apply'), self.now), -20.12)
        with self.assertRaises(ValueError):
            protocol.check_apply(data, self.source.replace('/cmd', '/apply'), self.now + timedelta(seconds=300))

    def test_invalid_json_and_numbers(self):
        for payload in ('{"a":1,"a":2}', '{"a":NaN}', '[]', 'x' * 16385):
            with self.subTest(payload=payload[:30]), self.assertRaises(ValueError):
                protocol.loads(payload)
        for value in (True, '-25', float('inf'), float('nan')):
            self.assertFalse(protocol.number(value))

    def test_retained_only_lock(self):
        protocol.delivery('lock', 1, True)
        for channel in ('cmd', 'apply', 'door', 'telemetry'):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                protocol.delivery(channel, 1, True)

    def test_telemetry_boundary_and_identity(self):
        data = dict(site_id='site01', device_id='fz01', seq=1,
                    measured_at=protocol.utc(self.now), temp_c=-25,
                    setpoint_c=-30, door_open=False, cooling=True, status='normal')
        source = self.source.replace('/cmd', '/telemetry')
        self.assertEqual(protocol.telemetry(data, source)['status'], 'normal')
        data['temp_c'] = -24.9
        with self.assertRaises(ValueError):
            protocol.telemetry(data, source)
        data['status'] = 'high_temp'
        protocol.telemetry(data, source)
        data['device_id'] = 'fz02'
        with self.assertRaises(ValueError):
            protocol.telemetry(data, source)


class AnalysisTests(unittest.TestCase):
    def sample(self, old=None, ts=0, temp=-30, door=True, received=None):
        return analyze(old, dict(ts=ts, temp=temp, door=door), ts if received is None else received, 15)[0]

    def test_hot_boundary_even_closed(self):
        self.assertEqual(self.sample(temp=-25, door=False)['status'], 'normal')
        self.assertEqual(self.sample(temp=-24.9, door=False)['status'], 'hot')

    def test_prediction_requires_window_and_hold(self):
        state = None
        for second in range(71):
            state = self.sample(state, second, -29 + second * .04)
            self.assertEqual(state['status'], 'early' if second == 70 else 'normal')

    def test_flat_or_falling_no_prediction(self):
        for slope in (0, -.01):
            state = None
            for second in range(80):
                state = self.sample(state, second, -29 + second * slope)
            self.assertIsNone(state['eta'])
            self.assertEqual(state['status'], 'normal')

    def test_old_sample_does_not_rewind(self):
        state = self.sample(ts=10)
        self.assertIs(self.sample(state, ts=9), state)

    def test_stale_and_gap(self):
        self.assertEqual(self.sample(received=16)['status'], 'stale')
        state = self.sample(ts=0)
        state = self.sample(state, ts=20)
        self.assertEqual(len(state['history']), 1)

    def test_close_and_reopen(self):
        state = self.sample(temp=-27)
        state = self.sample(state, 1, -27.1, False)
        self.assertEqual(state['status'], 'normal')
        self.assertIsNone(state['eta'])
        state = self.sample(state, 2, -27.1, True)
        self.assertEqual(state['status'], 'early')


class LauncherTests(unittest.TestCase):
    def test_shared_credentials_rejected(self):
        with patch.dict('os.environ', {'MQTT_PASS': 'dummy'}, clear=True):
            with self.assertRaises(ValueError):
                launcher.preflight()

    def test_invalid_config_starts_no_process(self):
        with patch.object(launcher, 'preflight', side_effect=ValueError('test')), patch.object(launcher.subprocess, 'Popen') as start:
            self.assertEqual(launcher.main(), 2)
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
