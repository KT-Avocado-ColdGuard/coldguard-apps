"""통제 API 호출. 브라우저 대신 이 서버가 kt-ui 키로 부른다.

- 자동 재시도·리다이렉트를 하지 않는다(명령이 두 번 나가거나 키가 다른 주소로 새지 않게).
- 환경변수 프록시(HTTPS_PROXY 등)를 쓰지 않는다. 통제 API는 테일넷으로 바로 부른다.
"""
import http.client
import json
import ssl
import urllib.error
import urllib.request

from control_ui.accounts import check_private

GET_TIMEOUT = 10.0
POST_TIMEOUT = 15.0     # 통제 API는 브로커 수신 확인을 최대 5초 기다린 뒤 응답한다
MAX_BYTES = 1 << 20     # 응답 본문 상한(장비·명령 목록은 수십 KB 안팎)
MIN_KEY_LEN = 32        # 통제 API 키 규칙과 같다


class Unreachable(Exception):
    """응답을 받지 못했다. sent가 거짓이면 요청이 통제 API에 닿지 않았고(연결 실패),
    참이면 요청은 다 보냈지만 응답을 받지 못했다(처리됐는지 알 수 없음)."""

    def __init__(self, sent, error):
        super().__init__(error)
        self.sent, self.error = sent, error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 3xx를 따라가지 않고 그대로 돌려준다


def read_key(path):
    """키 파일에서 키 한 줄을 읽는다. 앞뒤 공백·줄바꿈(Windows의 \\r 포함)은 지운다."""
    check_private(path)
    with open(path, encoding='utf-8-sig') as f:
        key = f.read().strip()
    if len(key) < MIN_KEY_LEN or not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        raise ValueError(f'API 키 파일에는 키 한 줄({MIN_KEY_LEN}자 이상, 공백 없는 ASCII)만 둡니다.')
    return key


class ControlApi:
    def __init__(self, base_url, key, opener=None):
        self._base = base_url
        self._auth = 'Bearer ' + key
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._opener = opener

    def call(self, method, path, body=None, auth=True):
        """(상태 코드, JSON 객체 또는 None). 응답을 못 받으면 Unreachable."""
        headers = {'Accept': 'application/json'}
        if auth:
            headers['Authorization'] = self._auth
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
        timeout = POST_TIMEOUT if method == 'POST' else GET_TIMEOUT
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, self._json(response)
        except urllib.error.HTTPError as exc:  # 4xx·5xx·3xx도 응답이다
            with exc:
                return exc.code, self._json(exc)
        except urllib.error.URLError as exc:
            # urllib은 연결·요청 전송 중의 오류만 URLError로 감싼다. 요청이 끝까지 나가지 않았다.
            raise Unreachable(False, type(exc.reason).__name__) from None
        except (OSError, http.client.HTTPException) as exc:
            # 요청을 다 보낸 뒤 응답을 기다리거나 읽다가 끊겼다(시간 초과 포함).
            raise Unreachable(True, type(exc).__name__) from None

    @staticmethod
    def _json(response):
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
