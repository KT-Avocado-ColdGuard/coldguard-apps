"""Evaluate an unfiltered Trivy report; no network or runtime credentials."""
import datetime as dt
import json
import sys

SERVICES = {'collector', 'controller', 'field-web'}

def evaluate(report, exceptions, image_id, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if report.get('SchemaVersion') != 2 or not isinstance(report.get('Results'), list) or not report['Results']:
        raise ValueError('Missing/unsupported scan results')
    if not isinstance(exceptions, list):
        raise ValueError('Exceptions must be a list')
    valid = []
    for e in exceptions:
        required = ('id', 'package', 'installed_version', 'target', 'image_id',
                    'approved_by', 'approval_issue', 'reason', 'mitigation', 'expires_at', 'services')
        if not all(e.get(k) for k in required):
            raise ValueError('Incomplete approval record')
        expiry = dt.date.fromisoformat(e['expires_at'])
        if set(e['services']) != SERVICES:
            raise ValueError('Shared image requires review of all three services')
        if e['image_id'] == image_id and today < expiry:
            valid.append(e)
    blocked, waived = [], []
    for result in report['Results']:
        for v in result.get('Vulnerabilities') or []:
            if v['Severity'] not in ('HIGH', 'CRITICAL'):
                continue
            match = any(e['id'] == v['VulnerabilityID'] and e['package'] == v['PkgName']
                        and e['installed_version'] == v['InstalledVersion']
                        and e['target'] == result['Target'] for e in valid)
            (waived if match else blocked).append({'target': result['Target'], **v})
    return {'blocked': blocked, 'waived': waived}

if __name__ == '__main__':
    try:
        report, exceptions, image_id, output = sys.argv[1:]
        result = evaluate(json.load(open(report)), json.load(open(exceptions)), image_id)
        with open(output, 'w') as f:
            json.dump(result, f, indent=2)
        print(f"Blocked: {len(result['blocked'])}; waived: {len(result['waived'])}")
        sys.exit(1 if result['blocked'] else 0)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f'Gate error: {exc}', file=sys.stderr)
        sys.exit(2)
