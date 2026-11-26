"""로그인 세션, 로그인 잠금, 명령 횟수 제한.

gunicorn 작업 프로세스 1개의 메모리에 둔다. 서버를 다시 시작하면 모두 다시 로그인한다.
시간은 단조 시계(time.monotonic)로 잰다(시스템 시각이 바뀌어도 만료가 흔들리지 않게).
"""
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

IDLE_SEC = 2 * 3600        # 이 시간 동안 사용하지 않으면 로그아웃
MAX_AGE_SEC = 24 * 3600    # 로그인 후 최대 유지 시간
FAIL_LIMIT = 5             # 연속 실패 횟수
LOCK_SEC = 5 * 60          # 잠금 시간
CMD_LIMIT = 10             # 사용자당 명령 요청 수
CMD_WINDOW_SEC = 60        # ... 이 시간 안에
UNKNOWN_TRACKED = 1000     # 없는 계정 ID의 실패 기록 상한(메모리 보호)


@dataclass
class Session:
    sid: str
    user: str
    csrf: str
    created: float
    last_active: float
    ended: str = None  # 만료가 확인되면 이유(idle·max_age). 동시에 온 다른 요청도 같은 이유를 받게 남겨 둔다.


class Sessions:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._items = {}

    def create(self, user):
        now = self._clock()
        session = Session(secrets.token_urlsafe(32), user, secrets.token_urlsafe(32), now, now)
        with self._lock:
            self._purge(now)
            self._items[session.sid] = session
        return session

    def get(self, sid, active):
        """(세션, None, None) 또는 (None, 이유, 사용자). 사용자는 만료를 처음 확인했을 때만 준다(로그를 한 번만 남기게).
        이유: missing, idle(미사용 시간 초과), max_age(최대 유지 시간 초과).
        active가 참일 때만 사용 시각을 갱신한다(자동 갱신 요청으로는 로그인이 연장되지 않게)."""
        if not sid:
            return None, 'missing', None
        now = self._clock()
        with self._lock:
            session = self._items.get(sid)
            if session is None:
                return None, 'missing', None
            reason = session.ended or self._expired(session, now)
            if reason:
                first = session.ended is None
                session.ended = reason  # 끝난 세션은 다음 로그인 때 정리한다(_purge)
                return None, reason, session.user if first else None
            if active:
                session.last_active = now
            return session, None, None

    def drop(self, sid):
        with self._lock:
            return self._items.pop(sid, None) if sid else None

    def drop_users(self, users):
        """계정이 지워졌거나 비밀번호가 바뀐 사용자의 세션을 끝낸다."""
        with self._lock:
            for sid in [s.sid for s in self._items.values() if s.user in users]:
                del self._items[sid]

    def count(self):
        with self._lock:
            return len(self._items)

    @staticmethod
    def _expired(session, now):
        if now - session.created >= MAX_AGE_SEC:
            return 'max_age'
        if now - session.last_active >= IDLE_SEC:
            return 'idle'
        return None

    def _purge(self, now):
        for sid in [s.sid for s in self._items.values() if self._expired(s, now)]:
            del self._items[sid]


class LoginGuard:
    """계정 ID별 연속 실패 5회면 5분 잠금. 없는 계정 ID도 똑같이 잠가서 계정 존재 여부가 드러나지 않게 한다."""

    def __init__(self, known, clock=time.monotonic):
        self._known = known            # 있는 계정인지 묻는 함수(있는 계정의 기록은 지우지 않는다)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = OrderedDict()    # user -> [연속 실패 수, 잠금 해제 시각]

    def locked(self, user):
        """남은 잠금 시간(초). 잠기지 않았으면 0."""
        with self._lock:
            entry = self._state.get(user)
            return max(0.0, entry[1] - self._clock()) if entry else 0.0

    def failed(self, user):
        """실패를 기록하고, 이번 실패로 잠겼으면 잠금 시간(초)을 돌려준다."""
        now = self._clock()
        with self._lock:
            entry = self._state.setdefault(user, [0, 0.0])
            self._state.move_to_end(user)
            entry[0] += 1
            wait = 0.0
            if entry[0] >= FAIL_LIMIT:
                entry[:] = [0, now + LOCK_SEC]
                wait = float(LOCK_SEC)
            self._trim(now)
            return wait

    def succeeded(self, user):
        with self._lock:
            self._state.pop(user, None)

    def _trim(self, now):
        unknown = [u for u in self._state if not self._known(u)]
        for user in unknown[:max(0, len(unknown) - UNKNOWN_TRACKED)]:  # 오래된 것부터
            if self._state[user][1] <= now:  # 잠긴 기록은 남긴다
                del self._state[user]


class RateLimit:
    """사용자당 최근 window초 안의 요청이 limit건이면 더 받지 않는다."""

    def __init__(self, limit=CMD_LIMIT, window=CMD_WINDOW_SEC, clock=time.monotonic):
        self._limit, self._window, self._clock = limit, window, clock
        self._lock = threading.Lock()
        self._hits = {}

    def hit(self, user):
        """요청을 세고 0을 돌려준다. 한도에 닿았으면 세지 않고 다시 보낼 수 있을 때까지 남은 초를 돌려준다."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(user, deque())
            while hits and hits[0] <= now - self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return max(0.0, hits[0] + self._window - now)
            hits.append(now)
            return 0.0
