"""통제 API 설정. 환경변수로만 받고, 비밀번호는 코드와 로그에 남기지 않는다.

EKS에서는 클러스터 내부 Mosquitto 1883(평문, control-api 계정)과 중앙 MySQL(coldguard_api 계정)에 붙는다.
로컬에서 TLS 브로커(8883)에 붙어 볼 때만 MQTT_TLS=true와 MQTT_CA를 준다.
"""
import math
import os

from common.coldguard_protocol import MAX_TTL_SEC, identity


def _flag(name, value):
    value = str(value).strip().lower()
    if value not in ('true', 'false'):
        raise ValueError(name + '는 true 또는 false로 지정하세요.')
    return value == 'true'


def _port(name, value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(name + ' 범위 오류')
    return port


def load(env=None):
    env = os.environ if env is None else env
    cfg = dict(
        MQTT_HOST=env.get('MQTT_HOST', 'mosquitto'),
        MQTT_PORT=env.get('MQTT_PORT', '1883'),
        MQTT_USER=env.get('MQTT_USER', 'control-api'),
        MQTT_PASS=env.get('MQTT_PASS', ''),
        # 파드마다 달라야 한다(같은 ID로 두 개가 붙으면 브로커가 서로 끊는다). 배포에서는 파드 이름을 넣는다.
        MQTT_CLIENT_ID=env.get('MQTT_CLIENT_ID', ''),
        MQTT_TLS=env.get('MQTT_TLS', 'false'),
        MQTT_CA=env.get('MQTT_CA', ''),
        MYSQL_HOST=env.get('MYSQL_HOST', 'mysql'),
        MYSQL_PORT=env.get('MYSQL_PORT', '3306'),
        MYSQL_DB=env.get('MYSQL_DB', 'coldguard_central'),
        MYSQL_USER=env.get('MYSQL_USER', 'coldguard_api'),
        MYSQL_PASSWORD=env.get('MYSQL_PASSWORD', ''),
        # 명령을 보낼 수 있는 시설 목록. 시설을 새로 배포할 때 여기에 추가한다.
        SITES=env.get('SITES', ''),
        CMD_TTL_SEC=env.get('CMD_TTL_SEC', '60'),
        API_KEYS_FILE=env.get('API_KEYS_FILE', '/etc/control-api/keys.json'),
        METRICS_HOST=env.get('METRICS_HOST', '0.0.0.0'),
        METRICS_PORT=env.get('METRICS_PORT', '9204'),
    )
    missing = [k for k, v in cfg.items() if not str(v).strip() and k != 'MQTT_CA']
    if missing:
        raise ValueError('설정 누락: ' + ', '.join(missing))
    for key in ('MQTT_PORT', 'MYSQL_PORT', 'METRICS_PORT'):
        cfg[key] = _port(key, cfg[key])
    cfg['MQTT_TLS'] = _flag('MQTT_TLS', cfg['MQTT_TLS'])
    if cfg['MQTT_TLS'] and not cfg['MQTT_CA'].strip():
        raise ValueError('MQTT_TLS=true이면 MQTT_CA(CA 파일 경로)가 필요합니다.')
    sites = [s.strip() for s in cfg['SITES'].split(',') if s.strip()]
    if not sites or len(set(sites)) != len(sites):
        raise ValueError('SITES는 중복 없이 하나 이상 지정하세요. 예: site01')
    for site in sites:
        identity(site, 'fz1')  # 시설 ID 규칙은 시설 controller와 같은 공통 규칙으로 검사한다.
    cfg['SITES'] = tuple(sites)
    ttl = float(cfg['CMD_TTL_SEC'])
    if not math.isfinite(ttl) or not 0 < ttl <= MAX_TTL_SEC:
        raise ValueError(f'CMD_TTL_SEC는 0보다 크고 {MAX_TTL_SEC:g} 이하여야 합니다.')
    cfg['CMD_TTL_SEC'] = ttl
    return cfg
