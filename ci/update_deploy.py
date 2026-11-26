"""이번 CI에서 성공적으로 게시한 정확한 릴리스를 비공개 GitOps 저장소에 반영합니다.

여기서는 이미지를 다시 빌드하거나, 최신 태그를 조회하거나, SSH 또는 Kubernetes에 접근하지 않습니다.
"""
import base64
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.request

SOURCE_REPO = 'KT-Avocado-ColdGuard/coldguard-apps'
TARGET_REPO = 'KT-Avocado-ColdGuard/coldguard-deploy'
CENTRAL = {'central-worker': 'central-worker/statefulset.yaml',
           'control-api': 'control-api/deployment.yaml'}
FACILITY = ('collector', 'controller', 'field-web')
RELEASE_PATH = 'facility/releases/site01.json'


class UpdateError(Exception):
    pass


class API:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request('https://api.github.com/' + path, data=data,
            method=method, headers={'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json', 'User-Agent': 'coldguard-cd',
                'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # API 응답에는 비공개 저장소 데이터가 포함될 수 있으므로 출력하지 않습니다.
            raise UpdateError('GitHub API request failed: HTTP ' + str(exc.code)) from None


def read_release(text, sha, account, region):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise UpdateError('Invalid source SHA')
    if not re.fullmatch('[0-9]{12}', account) or not re.fullmatch('[a-z0-9-]+', region):
        raise UpdateError('Invalid registry settings')
    registry = account + '.dkr.ecr.' + region + '.amazonaws.com'
    rows = {}
    for row in csv.DictReader(io.StringIO(text)):
        service = row.get('service', '')
        if service in rows:
            raise UpdateError('Duplicate service in publish report')
        expected = registry + '/coldguard-prod-' + service + ':' + sha
        if row.get('image') != expected or not re.fullmatch('sha256:[0-9a-f]{64}', row.get('digest', '')):
            raise UpdateError('Publish report does not match this source and registry')
        if row.get('result') not in ('missing', 'identical'):
            raise UpdateError('Unexpected publish result')
        rows[service] = row
    if not (set(CENTRAL) | set(FACILITY)) <= rows.keys():
        raise UpdateError('Publish report is missing a required service')
    return rows


def replace_image(text, row):
    pattern = re.compile(r'^(\s*image:[ \t]*)([^\s#]+)([ \t]*(?:#.*)?)$', re.MULTILINE)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise UpdateError('Expected exactly one image in the deployment file')
    old = matches[0].group(2)
    repo = row['image'].rsplit(':', 1)[0]
    if not re.fullmatch(re.escape(repo) + r'(?::[0-9a-f]{40}|@sha256:[0-9a-f]{64})', old):
        raise UpdateError('Unexpected current image repository or tag')
    return pattern.sub(lambda m: m.group(1) + row['image'] + m.group(3), text)


def get_text(api, path, ref, optional=False):
    try:
        value = api.call('GET', 'repos/' + TARGET_REPO + '/contents/' + path + '?ref=' + ref)
    except UpdateError as exc:
        if optional and str(exc).endswith('HTTP 404'):
            return None
        raise
    if value.get('type') != 'file' or value.get('encoding') != 'base64':
        raise UpdateError('Unexpected repository content response')
    return base64.b64decode(value['content']).decode('utf-8')


def update(target, source, rows, sha, run_url):
    source_path = 'repos/' + SOURCE_REPO + '/git/ref/heads/main'
    target_path = 'repos/' + TARGET_REPO
    for attempt in range(3):
        if source.call('GET', source_path)['object']['sha'] != sha:
            return 'skipped: a newer source main commit exists'
        base = target.call('GET', target_path + '/git/ref/heads/main')['object']['sha']
        commit = target.call('GET', target_path + '/git/commits/' + base)
        files = {}
        for service, path in CENTRAL.items():
            old = get_text(target, path, base)
            new = replace_image(old, rows[service])
            if old != new:
                files[path] = new
        release = {'schema_version': 1, 'source_repository': SOURCE_REPO,
                   'source_sha': sha, 'ci_run_url': run_url,
                   'app_images': {s: rows[s]['image'].rsplit(':', 1)[0] + '@' + rows[s]['digest']
                                  for s in FACILITY}}
        old_release = get_text(target, RELEASE_PATH, base, optional=True)
        if old_release is None or json.loads(old_release) != release:
            files[RELEASE_PATH] = json.dumps(release, indent=2) + '\n'
        if not files:
            return 'unchanged: release already recorded'
        tree = target.call('POST', target_path + '/git/trees', {
            'base_tree': commit['tree']['sha'],
            'tree': [{'path': p, 'mode': '100644', 'type': 'blob', 'content': c}
                     for p, c in files.items()]})
        new_commit = target.call('POST', target_path + '/git/commits', {
            'message': 'chore: deploy approved apps ' + sha,
            'tree': tree['sha'], 'parents': [base]})
        # 다른 작성자의 변경 사항을 덮어쓰지 않으며, 강제 푸시도 하지 않습니다.
        if source.call('GET', source_path)['object']['sha'] != sha:
            return 'skipped: a newer source main commit exists'
        if target.call('GET', target_path + '/git/ref/heads/main')['object']['sha'] != base:
            time.sleep(attempt + 1)
            continue
        try:
            target.call('PATCH', target_path + '/git/refs/heads/main',
                        {'sha': new_commit['sha'], 'force': False})
        except UpdateError as exc:
            if str(exc).endswith(('HTTP 409', 'HTTP 422')) and attempt < 2:
                continue
            raise
        return 'updated: ' + new_commit['sha']
    raise UpdateError('Deployment repository changed concurrently; retry this CI run')


def main():
    try:
        sha = os.environ['GITHUB_SHA']
        with open('reports/published-images.csv', encoding='utf-8', newline='') as source_file:
            rows = read_release(source_file.read(), sha, os.environ['AWS_ACCOUNT_ID'], os.environ['AWS_REGION'])
        result = update(API(os.environ['DEPLOY_TOKEN']), API(os.environ['SOURCE_TOKEN']),
                        rows, sha, os.environ['CI_RUN_URL'])
        print(result)
        with open('reports/gitops-update.txt', 'w', encoding='utf-8') as result_file:
            result_file.write(result + '\n')
        return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc, UpdateError) else type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
