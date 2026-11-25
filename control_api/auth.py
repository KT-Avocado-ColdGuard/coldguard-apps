"""API 키 인증과 권한.

키 파일(JSON 배열, 쿠버네티스 Secret으로 마운트)의 한 항목:
  {"name": "kt-ui", "key": "<32자 이상 무작위 문자열>", "scope": "command", "sites": ["site01"]}
- name: 명령 기록(issued_by)에 남는 호출 주체 이름. 영문 소문자·숫자·하이픈, 19자 이하
- scope: read(조회만) 또는 command(조회 + 명령)
- sites: 이 키로 조회·명령할 수 있는 시설. 설정 SITES 안에 있어야 한다.
키를 바꾸면 파드를 다시 시작해야 반영된다.
"""
import hmac
import json
import re
from dataclasses import dataclass

NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,18}')
SCOPES = ('read', 'command')
MIN_KEY_LEN = 32


@dataclass(frozen=True)
class Principal:
    name: str
    scope: str
    sites: tuple

    def can_command(self):
        return self.scope == 'command'


class KeyRing:
    def __init__(self, entries, allowed_sites):
        if not isinstance(entries, list) or not entries:
            raise ValueError('API 키 파일은 항목이 하나 이상인 JSON 배열이어야 합니다.')
        names, secrets = set(), set()
        self._keys = []
        for i, e in enumerate(entries):
            where = f'API 키 {i + 1}번 항목'
            if not isinstance(e, dict) or set(e) != {'name', 'key', 'scope', 'sites'}:
                raise ValueError(where + ': name, key, scope, sites 네 필드만 둡니다.')
            name, key, scope, sites = e['name'], e['key'], e['scope'], e['sites']
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise ValueError(where + ': name은 영문 소문자·숫자·하이픈 19자 이하')
            if not isinstance(key, str) or len(key) < MIN_KEY_LEN or not key.isascii() or not key.isprintable():
                raise ValueError(where + f': key는 {MIN_KEY_LEN}자 이상 출력 가능한 ASCII 문자열')
            if scope not in SCOPES:
                raise ValueError(where + ': scope는 read 또는 command')
            if (not isinstance(sites, list) or not sites or len(set(sites)) != len(sites)
                    or any(s not in allowed_sites for s in sites)):
                raise ValueError(where + ': sites는 설정 SITES 안의 시설을 중복 없이 하나 이상')
            if name in names or key in secrets:
                raise ValueError(where + ': name과 key는 항목마다 달라야 합니다.')
            names.add(name)
            secrets.add(key)
            self._keys.append((key.encode(), Principal(name, scope, tuple(sites))))

    @classmethod
    def from_file(cls, path, allowed_sites):
        with open(path, encoding='utf-8') as f:
            return cls(json.load(f), allowed_sites)

    def principals(self):
        return [p for _, p in self._keys]

    def lookup(self, token):
        """일치하는 키의 주체를 돌려준다. 키마다 같은 시간 비교를 끝까지 해서 일치 위치가 드러나지 않게 한다."""
        if not isinstance(token, str) or not token.isascii():
            return None
        given = token.encode()
        found = None
        for key, principal in self._keys:
            if hmac.compare_digest(given, key) and found is None:
                found = principal
        return found


def bearer(header):
    """Authorization 헤더에서 키를 꺼낸다. 형식이 틀리면 None."""
    if not header:
        return None
    scheme, _, token = header.partition(' ')
    token = token.strip()
    if scheme.lower() != 'bearer' or not token:
        return None
    return token
