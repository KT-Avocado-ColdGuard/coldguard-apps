"""통제 UI 계정 파일과 계정 도구.

계정 파일(JSON 객체, 권한 600): {"<계정ID>": "<비밀번호 해시>", ...}
- 계정 ID: 영문 소문자·숫자·밑줄·하이픈 3~30자. 명령 기록에 kt-ui:<계정ID>로 남는다(통제 API operator는 30자 이하).
- 해시: scrypt(werkzeug). 비밀번호 원문은 어디에도 저장하지 않는다.
- 비밀번호 규칙: 12자 이상, 영문 대문자·소문자·숫자·기호를 모두 넣고, 계정 ID는 넣지 않는다.

계정 도구(비밀번호는 계정 주인이 직접 입력한다):
  python -m control_ui.accounts --file <계정 파일> add <계정ID>
  python -m control_ui.accounts --file <계정 파일> passwd <계정ID>
  python -m control_ui.accounts --file <계정 파일> remove <계정ID>
  python -m control_ui.accounts --file <계정 파일> list
서버는 파일이 바뀌면 다시 읽는다(재시작 불필요). 지우거나 비밀번호를 바꾼 계정의 로그인 세션은 끝난다.
"""
import argparse
import getpass
import json
import os
import re
import secrets
import string
import sys
import tempfile
import threading

from werkzeug.security import check_password_hash, generate_password_hash

from common.coldguard_runtime import log

USER = re.compile(r'[a-z0-9][a-z0-9_-]{2,29}')
MIN_LEN, MAX_LEN = 12, 128
SYMBOLS = frozenset(string.punctuation)
METHOD = 'scrypt'


def password_problems(password, user=None):
    """지키지 않은 비밀번호 규칙 목록(비었으면 통과)."""
    problems = []
    if len(password) < MIN_LEN:
        problems.append(f'{MIN_LEN}자 이상')
    if len(password) > MAX_LEN:
        problems.append(f'{MAX_LEN}자 이하')
    if not any('a' <= c <= 'z' for c in password):
        problems.append('영문 소문자 포함')
    if not any('A' <= c <= 'Z' for c in password):
        problems.append('영문 대문자 포함')
    if not any('0' <= c <= '9' for c in password):
        problems.append('숫자 포함')
    if not any(c in SYMBOLS for c in password):
        problems.append('기호 포함')
    if not password.isprintable():
        problems.append('제어 문자 없음')
    if user and user in password.lower():
        problems.append('계정 ID 미포함')
    return problems


def check_private(path):
    """그룹·다른 사용자가 읽을 수 있는 비밀 파일은 쓰지 않는다(ssh 개인 키와 같은 기준). POSIX에서만 검사한다."""
    if os.name == 'posix' and os.stat(path).st_mode & 0o077:
        raise ValueError(f'{path}: 그룹·다른 사용자도 읽을 수 있습니다. 권한을 600으로 바꾸세요.')


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f'계정 ID가 두 번 나옵니다: {key}')
        seen[key] = value
    return seen


def parse(text):
    data = json.loads(text, object_pairs_hook=_no_duplicates)
    if not isinstance(data, dict):
        raise ValueError('계정 파일은 JSON 객체({"계정ID": "해시"})여야 합니다.')
    for user, hashed in data.items():
        if not USER.fullmatch(user):
            raise ValueError('계정 ID는 영문 소문자·숫자·밑줄·하이픈 3~30자입니다.')
        if not isinstance(hashed, str) or not hashed.startswith(METHOD + ':'):
            raise ValueError(f'{user}: 비밀번호 해시 형식이 아닙니다. 계정 도구로 만드세요.')
    return data


def load(path):
    with open(path, encoding='utf-8-sig') as f:
        return parse(f.read())


def save(path, accounts):
    """같은 폴더의 임시 파일에 쓴 뒤 바꿔 넣는다(쓰는 도중 서버가 반쯤 쓴 파일을 읽지 않게). 권한은 600."""
    directory = os.path.dirname(os.path.abspath(path))
    owner = None
    if os.path.exists(path) and hasattr(os, 'chown'):
        st = os.stat(path)
        owner = (st.st_uid, st.st_gid)
    fd, tmp = tempfile.mkstemp(prefix='.accounts-', dir=directory)  # mkstemp는 0600으로 만든다
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        if owner:
            try:
                os.chown(tmp, *owner)  # root로 고쳐도 컨테이너 사용자(uid 10001)가 계속 읽을 수 있게 주인을 유지
            except PermissionError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class AccountBook:
    """서버가 쓰는 계정 목록. 파일이 바뀌면 다시 읽는다."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._sig = self._bad_sig = None
        # 없는 계정도 같은 시간이 걸리도록 비교에 쓰는 해시(계정 존재 여부가 응답 시간으로 드러나지 않게)
        self._dummy = generate_password_hash(secrets.token_urlsafe(24), method=METHOD)
        check_private(path)
        self._sig = self._signature()
        self._users = load(path)
        if not self._users:
            raise ValueError('계정이 없습니다. 계정 도구로 먼저 만드세요(control_ui/README.md).')

    def _signature(self):
        st = os.stat(self._path)
        return st.st_ino, st.st_size, st.st_mtime_ns

    def users(self):
        return sorted(self._users)

    def has(self, user):
        return user in self._users

    def refresh(self):
        """파일이 바뀌었으면 다시 읽고, 지워졌거나 비밀번호가 바뀐 계정 ID를 돌려준다.
        새 파일에 오류가 있으면 기존 목록을 그대로 쓴다(오류는 한 번만 기록)."""
        try:
            sig = self._signature()
        except OSError:
            sig = 'missing'
        with self._lock:
            if sig in (self._sig, self._bad_sig):
                return set()
            try:
                if sig == 'missing':
                    raise ValueError('계정 파일을 읽을 수 없습니다.')
                check_private(self._path)
                users = load(self._path)
            except (OSError, ValueError, UnicodeError) as exc:
                log('control_ui_accounts_error', reason=str(exc))
                self._bad_sig = sig
                return set()
            changed = {u for u, h in self._users.items() if users.get(u) != h}
            self._users, self._sig, self._bad_sig = users, sig, None
        log('control_ui_accounts_loaded', users=sorted(users), ended=sorted(changed))
        return changed

    def verify(self, user, password):
        hashed = self._users.get(user)
        if hashed is None:
            check_password_hash(self._dummy, password)
            return False
        return check_password_hash(hashed, password)


# ---------- 계정 도구 ----------

class ToolError(Exception):
    pass


def _ask_password(user, prompt):
    first = prompt(f'{user} 새 비밀번호: ')
    problems = password_problems(first, user)
    if problems:
        raise ToolError('비밀번호 규칙을 지키지 않았습니다: ' + ', '.join(problems))
    if prompt('한 번 더 입력: ') != first:
        raise ToolError('두 번 입력한 비밀번호가 다릅니다.')
    return first


def main(argv=None, prompt=getpass.getpass, out=sys.stdout):
    parser = argparse.ArgumentParser(prog='python -m control_ui.accounts', description='통제 UI 계정 관리')
    parser.add_argument('--file', default=os.environ.get('ACCOUNTS_FILE', '/etc/control-ui/accounts.json'),
                        help='계정 파일 경로(기본: ACCOUNTS_FILE 환경변수)')
    commands = parser.add_subparsers(dest='command', required=True)
    for name, text in (('add', '계정 추가'), ('passwd', '비밀번호 변경'), ('remove', '계정 삭제')):
        commands.add_parser(name, help=text).add_argument('user', help='계정 ID')
    commands.add_parser('list', help='계정 ID 목록')
    args = parser.parse_args(argv)

    try:
        exists = os.path.exists(args.file)
        if args.command != 'add' and not exists:
            raise ToolError(f'계정 파일이 없습니다: {args.file}')
        accounts = load(args.file) if exists else {}
        if args.command == 'list':
            for user in sorted(accounts):
                print(user, file=out)
            return 0
        user = args.user
        if not USER.fullmatch(user):
            raise ToolError('계정 ID는 영문 소문자·숫자·밑줄·하이픈 3~30자입니다.')
        if args.command == 'add' and user in accounts:
            raise ToolError(f'이미 있는 계정입니다: {user} (비밀번호 변경은 passwd)')
        if args.command in ('passwd', 'remove') and user not in accounts:
            raise ToolError(f'없는 계정입니다: {user}')
        if args.command == 'remove':
            del accounts[user]
        else:
            accounts[user] = generate_password_hash(_ask_password(user, prompt), method=METHOD)
        save(args.file, accounts)
    except (ToolError, ValueError, OSError) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 1
    print({'add': '추가했습니다', 'passwd': '비밀번호를 바꿨습니다', 'remove': '삭제했습니다'}[args.command]
          + f': {user} ({args.file})', file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
