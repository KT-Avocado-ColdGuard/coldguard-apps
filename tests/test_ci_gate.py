import copy
import datetime as dt
import unittest
import sys
from ci.gate import evaluate

class GateTests(unittest.TestCase):
    def setUp(self):
        self.report = {'SchemaVersion': 2, 'Results': [{'Target': 'os', 'Vulnerabilities': [
            {'VulnerabilityID': 'TEST-1', 'PkgName': 'pkg', 'InstalledVersion': '1', 'Severity': 'HIGH'}]}]}
        self.exception = dict(id='TEST-1', package='pkg', installed_version='1', target='os',
            image_id='sha256:test', approved_by='test reviewer', approval_issue='test-only',
            reason='fixture', mitigation='fixture', expires_at='2030-02-01',
            services=['collector', 'controller', 'field-web', 'central-worker'])
    def check(self, exceptions):
        return evaluate(self.report, exceptions, 'sha256:test', dt.date(2030, 1, 1))
    def test_high_and_critical_block(self):
        for severity in ('HIGH', 'CRITICAL'):
            self.report['Results'][0]['Vulnerabilities'][0]['Severity'] = severity
            self.assertEqual(len(self.check([])['blocked']), 1)
    def test_medium_passes(self):
        self.report['Results'][0]['Vulnerabilities'][0]['Severity'] = 'MEDIUM'
        self.assertFalse(self.check([])['blocked'])
    def test_valid_exception(self):
        self.assertFalse(self.check([self.exception])['blocked'])
    def test_expired_and_other_image_block(self):
        for field, value in [('expires_at', '2030-01-01'), ('image_id', 'other')]:
            e = {**self.exception, field: value}
            self.assertTrue(self.check([e])['blocked'])
    def test_missing_approval_and_partial_service_fail(self):
        for field, value in [('approved_by', ''), ('services', ['collector'])]:
            with self.assertRaises(ValueError): self.check([{**self.exception, field: value}])
    def test_invalid_scan_fails(self):
        for report in ({}, {'SchemaVersion': 2, 'Results': []}):
            with self.assertRaises(ValueError): evaluate(report, [], 'image')
    def test_exception_without_central_worker_is_rejected(self):
        # 공통 이미지의 네 서비스 중 중앙 워커 승인이 빠지면 거부한다.
        exception = {
            **self.exception,
            'services': ['collector', 'controller', 'field-web'],
        }

        with self.assertRaises(ValueError):
            self.check([exception])
            
@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "Linux Bash process test; run in WSL or GitHub Actions",
)

class ScannerProcessTests(unittest.TestCase):
    def test_scanner_and_timeout_errors_preserve_logs(self):
        import os
        import pathlib
        import subprocess
        import tempfile
        script = pathlib.Path('ci/scan.sh').resolve()
        for code in (1, 124):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                exe = pathlib.Path(tmp) / 'trivy'
                exe.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo mock; exit 0; fi\necho "mock DB download/scanner error" >&2\nexit {code}\n')
                exe.chmod(0o755)
                result = subprocess.run(['bash', str(script), 'test-image'], cwd=tmp,
                    env={**os.environ, 'PATH': tmp + ':' + os.environ['PATH']}, capture_output=True)
                self.assertEqual(result.returncode, code)
                self.assertIn('mock DB', (pathlib.Path(tmp)/'reports/trivy-scan.log').read_text())
