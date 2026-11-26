"""통제 UI 설정. 환경변수로만 받는다. API 키와 계정은 파일 경로만 받고, 내용은 로그에 남기지 않는다."""
import math
import os
from urllib.parse import urlsplit


def _port(name, value):
    try:
        port = int(value)
    except ValueError:
        raise ValueError(name + '는 정수로 지정하세요.') from None
    if not 1 <= port <= 65535:
        raise ValueError(name + ' 범위 오류')
    return port


def load(env=None):
    env = os.environ if env is None else env
    cfg = dict(
        # 통제 API 주소(https, 경로 없이). 허브에서는 테일넷 주소를 쓴다.
        CONTROL_API_URL=env.get('CONTROL_API_URL', ''),
        # kt-ui 키 한 줄만 든 파일(권한 600)
        API_KEY_FILE=env.get('API_KEY_FILE', '/etc/control-ui/api-key'),
        # 계정 파일(권한 600). 만드는 방법은 control_ui/README.md 참고
        ACCOUNTS_FILE=env.get('ACCOUNTS_FILE', '/etc/control-ui/accounts.json'),
        # 측정이 이보다 오래되면 "확인 불가"로 표시한다. 시설 collector·현장 웹의 STALE_SEC와 같은 값을 넣는다.
        STALE_SEC=env.get('STALE_SEC', ''),
        # 장비별 Grafana 대시보드 주소. {site}와 {device} 자리에 시설·장비 ID가 들어간다. 비우면 링크를 숨긴다.
        GRAFANA_DEVICE_URL=env.get('GRAFANA_DEVICE_URL', ''),
        METRICS_HOST=env.get('METRICS_HOST', '0.0.0.0'),
        METRICS_PORT=env.get('METRICS_PORT', '9205'),
    )
    missing = [k for k, v in cfg.items() if not str(v).strip() and k != 'GRAFANA_DEVICE_URL']
    if missing:
        raise ValueError('설정 누락: ' + ', '.join(missing))

    url = cfg['CONTROL_API_URL'].strip().rstrip('/')
    parts = urlsplit(url)
    try:
        parts.port  # 포트 자리에 숫자가 아닌 값이 있으면 여기서 ValueError
    except ValueError:
        parts = None
    if (parts is None or parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment):
        raise ValueError('CONTROL_API_URL은 https://호스트 형식(경로 없이)으로 지정하세요.')
    cfg['CONTROL_API_URL'] = url

    try:
        stale = float(cfg['STALE_SEC'])
    except ValueError:
        stale = math.nan
    if not math.isfinite(stale) or stale <= 0:
        raise ValueError('STALE_SEC에 시설과 같은 데이터 단절 기준(초)을 지정하세요.')
    cfg['STALE_SEC'] = stale

    grafana = cfg['GRAFANA_DEVICE_URL'].strip()
    if grafana:
        parts = urlsplit(grafana)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or '{site}' not in grafana or '{device}' not in grafana):
            raise ValueError('GRAFANA_DEVICE_URL은 http(s) 주소이고 {site}와 {device}를 넣어야 합니다.')
    cfg['GRAFANA_DEVICE_URL'] = grafana

    cfg['METRICS_PORT'] = _port('METRICS_PORT', cfg['METRICS_PORT'])
    return cfg
