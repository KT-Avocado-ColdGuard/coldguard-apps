import base64
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('update_deploy', Path(__file__).parents[1] / 'ci/update_deploy.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
SHA = 'a' * 40
ACCOUNT = '123456789012'
REGISTRY = ACCOUNT + '.dkr.ecr.ap-northeast-2.amazonaws.com'


def report():
    return 'service,image,digest,result\n' + ''.join(
        name + ',' + REGISTRY + '/coldguard-prod-' + name + ':' + SHA + ',sha256:' + 'b' * 64 + ',missing\n'
        for name in ('collector', 'controller', 'field-web', 'central-worker', 'control-api'))


class Source:
    def __init__(self, sha=SHA):
        self.sha = sha
    def call(self, method, path, body=None):
        return {'object': {'sha': self.sha}}


class Target:
    def __init__(self):
        self.writes = []
    def call(self, method, path, body=None):
        if method != 'GET':
            self.writes.append((method, path, body))
            return {'sha': 'c' * 40}
        if '/git/ref/' in path:
            return {'object': {'sha': 'd' * 40}}
        if '/git/commits/' in path:
            return {'tree': {'sha': 'e' * 40}}
        if app.RELEASE_PATH in path:
            raise app.UpdateError('GitHub API request failed: HTTP 404')
        service = 'central-worker' if 'central-worker/' in path else 'control-api'
        text = 'spec:\n  image: ' + REGISTRY + '/coldguard-prod-' + service + ':' + 'f' * 40 + '\n'
        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}


class UpdateTests(unittest.TestCase):
    def rows(self):
        return app.read_release(report(), SHA, ACCOUNT, 'ap-northeast-2')

    def test_validate_exact_report(self):
        self.assertEqual(len(self.rows()), 5)

    def test_reject_wrong_source_registry_digest_and_duplicates(self):
        bads = [report().replace(SHA, 'f' * 40), report().replace(ACCOUNT, '000000000000'),
                report().replace('sha256:', 'bad:'), report() + report().splitlines()[1] + '\n']
        for bad in bads:
            with self.assertRaises(app.UpdateError):
                app.read_release(bad, SHA, ACCOUNT, 'ap-northeast-2')

    def test_only_target_image_changes(self):
        row = self.rows()['control-api']
        old = '  image: ' + row['image'].replace(SHA, 'f' * 40) + '\n  command: [python]\n'
        new = app.replace_image(old, row)
        self.assertEqual(new, old.replace('f' * 40, SHA))

    def test_ambiguous_or_wrong_repository_is_rejected(self):
        row = self.rows()['control-api']
        for old in ('image: other:tag\n', 'image: x\nimage: y\n'):
            with self.assertRaises(app.UpdateError):
                app.replace_image(old, row)

    def test_superseded_source_never_writes(self):
        target = Target()
        result = app.update(target, Source('f' * 40), self.rows(), SHA, 'https://example.invalid')
        self.assertTrue(result.startswith('skipped'))
        self.assertEqual(target.writes, [])

    def test_atomic_commit_contains_only_two_images_and_facility_release(self):
        target = Target()
        app.update(target, Source(), self.rows(), SHA, 'https://example.invalid')
        tree = target.writes[0][2]['tree']
        self.assertEqual({f['path'] for f in tree}, set(app.CENTRAL.values()) | {app.RELEASE_PATH})
        release = json.loads(next(f['content'] for f in tree if f['path'] == app.RELEASE_PATH))
        self.assertEqual(set(release['app_images']), set(app.FACILITY))
        self.assertTrue(all('@sha256:' in image for image in release['app_images'].values()))
        self.assertFalse(target.writes[-1][2]['force'])


if __name__ == '__main__':
    unittest.main()
