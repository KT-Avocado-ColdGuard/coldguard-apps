"""Discover public GHCR tags; commit hashes are opaque strings, not dates."""
import json
import os
import re
import urllib.parse
import urllib.request

SERVICES = ('simulator', 'collector', 'controller', 'field-web')

def entries(service, tags, selected=''):
    if not isinstance(tags, list) or not tags:
        raise ValueError(f'No tags for {service}')
    if selected and selected not in tags:
        raise ValueError(f'{service}: requested tag missing: {selected}')
    result = []
    for tag in sorted(set(tags)):
        if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
            raise ValueError('Invalid registry tag')
        if not selected or tag == selected:
            result.append({'service': service, 'tag': tag})
    return result

def discover(selected=''):
    result = []
    for service in SERVICES:
        repo = 'kt-avocado-coldguard/coldguard-' + service
        query = urllib.parse.urlencode({'scope': f'repository:{repo}:pull', 'service': 'ghcr.io'})
        with urllib.request.urlopen('https://ghcr.io/token?' + query, timeout=30) as response:
            token = json.load(response)['token']
        url = f'https://ghcr.io/v2/{repo}/tags/list?n=100'
        tags = []
        while url:
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=30) as response:
                tags.extend(json.load(response).get('tags') or [])
                link = response.headers.get('Link', '')
            match = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
            url = urllib.parse.urljoin(url, match.group(1)) if match else None
            if url and urllib.parse.urlparse(url).netloc != 'ghcr.io':
                raise ValueError('Unexpected pagination host')
        result.extend(entries(service, tags, selected))
    if len(result) > 256:
        raise ValueError('More than 256 tags: use manual tag selection or split audit batches')
    return {'include': result}

if __name__ == '__main__':
    matrix = discover(os.environ.get('IMAGE_TAG', ''))
    print(json.dumps(matrix))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix) + '\n')
