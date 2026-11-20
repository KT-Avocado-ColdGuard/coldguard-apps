"""실행 설정: 아래 변수 값을 수정하고 프로그램을 다시 실행하세요. 주소 / 번호 / 인증서 / 상태 경로까지 미리 지정합니다. 실행 시 입력을 요구하지 않습니다."""
import os
from pathlib import Path

# ---------- 이 부분을 수정하세요 ----------
MQTT_HOST = ""  # 부여받은 MQTT 브로커 IP 또는 DNS 이름
MQTT_PORT = 8883
MYSQL_HOST = ""  # 부여받은 MySQL IP 또는 DNS 이름
MYSQL_PORT = 3306
WEB_HOST = "0.0.0.0"  # 모든 로컬 인터페이스에서 수신. 접속 주소가 아닙니다.
SIM_WEB_PORT = 5000
FIELD_WEB_PORT = 8080
METRICS_HOST = "0.0.0.0"
COLLECTOR_METRICS_PORT = 9201
CONTROLLER_METRICS_PORT = 9202
SITE = "site01"  # 숫자만 쓰지 말고 site03처럼 전체 ID 지정
DEVICE = "fz01"  # 이 시뮬레이터 한 대의 ID / MQTT 계정 이름
DEVICES = "fz01"  # 시설 서비스가 관리할 장비 목록. 예: "fz01,fz02"
SIM_MQTT = False  # 실제 연결 시 True
MQTT_CA = ""  # 실행 환경 안의 CA 경로. 예: /etc/coldguard/ca.crt
MYSQL_DB = "coldguard"
STALE_SEC = ""  # 합의한 데이터 단절 기준(초)
WEB_ALLOW_NETS = ""  # 접속 허용 CIDR. 배포 네트워크에 맞게 지정
WEB_SECRET = ""  # 32자 이상. 실제 값은 Git에 올리지 마세요.
WEB_MQTT = True
WEB_CONTROL = True

# 서비스마다 발급받은 인증정보. 실제 비밀번호를 입력한 파일은 공유하지 마세요.
SIM_PASSWORD = ""
COLLECTOR_USER = "collector"
COLLECTOR_PASSWORD = ""
CONTROLLER_USER = "controller"
CONTROLLER_PASSWORD = ""
WEB_USER = "web"
WEB_PASSWORD = ""
COLLECTOR_DB_USER = "collector"
COLLECTOR_DB_PASSWORD = ""
CONTROLLER_DB_USER = "controller"
CONTROLLER_DB_PASSWORD = ""
WEB_DB_USER = "web"
WEB_DB_PASSWORD = ""

# 실행 위치와 무관하게 이 파일 옆의 state 폴더 사용. 배포 시 영속 경로로 변경 가능.
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
# ---------- 아래는 설정 조합용 코드 ----------
def values(role, env=None):
    """환경변수 우선 / 미설정 시 파일 기본값. 빈 환경변수도 명시적 설정으로 처리한다."""
    env = os.environ if env is None else env
    def get(key, default):
        return str(env[key]) if key in env else str(default)
    def flag(key, default):
        value = get(key, default).strip().lower()
        if value in ('1', 'true', 'yes', 'on'):
            return '1'
        if value in ('0', 'false', 'no', 'off'):
            return '0'
        raise ValueError(key + '은 true/false 또는 1/0으로 지정하세요.')
    site, device = get('SITE', SITE).strip(), get('DEVICE', DEVICE).strip()
    accounts = {
        'simulator': (device, get('SIM_PASSWORD', SIM_PASSWORD), '', ''),
        'collector': (get('COLLECTOR_USER', COLLECTOR_USER), get('COLLECTOR_PASSWORD', COLLECTOR_PASSWORD), get('COLLECTOR_DB_USER', COLLECTOR_DB_USER), get('COLLECTOR_DB_PASSWORD', COLLECTOR_DB_PASSWORD)),
        'controller': (get('CONTROLLER_USER', CONTROLLER_USER), get('CONTROLLER_PASSWORD', CONTROLLER_PASSWORD), get('CONTROLLER_DB_USER', CONTROLLER_DB_USER), get('CONTROLLER_DB_PASSWORD', CONTROLLER_DB_PASSWORD)),
        'web': (get('WEB_USER', WEB_USER), get('WEB_PASSWORD', WEB_PASSWORD), get('WEB_DB_USER', WEB_DB_USER), get('WEB_DB_PASSWORD', WEB_DB_PASSWORD)),
    }
    user, password, db_user, db_password = accounts[role]
    state = get('STATE_DIR', STATE_DIR)
    if not state.strip():
        raise ValueError('STATE_DIR는 빈 값일 수 없습니다.')
    client = f'sim-{site}-{device}' if role == 'simulator' else f'{role}-{site}'
    web_port = get('SIM_WEB_PORT', SIM_WEB_PORT) if role == 'simulator' else get('FIELD_WEB_PORT', FIELD_WEB_PORT)
    metrics_port = get('COLLECTOR_METRICS_PORT', COLLECTOR_METRICS_PORT) if role == 'collector' else get('CONTROLLER_METRICS_PORT', CONTROLLER_METRICS_PORT)
    result = dict(SITE=site, DEVICE=device, DEVICES=get('DEVICES', DEVICES),
        MQTT_HOST=get('MQTT_HOST', MQTT_HOST), MQTT_PORT=get('MQTT_PORT', MQTT_PORT), MQTT_CA=get('MQTT_CA', MQTT_CA),
        MQTT_USER=get('MQTT_USER', user), MQTT_PASS=get('MQTT_PASS', password), MQTT_CLIENT_ID=get('MQTT_CLIENT_ID', client),
        MYSQL_HOST=get('MYSQL_HOST', MYSQL_HOST), MYSQL_PORT=get('MYSQL_PORT', MYSQL_PORT), MYSQL_DB=get('MYSQL_DB', MYSQL_DB),
        MYSQL_USER=get('MYSQL_USER', db_user), MYSQL_PASSWORD=get('MYSQL_PASSWORD', db_password),
        WEB_HOST=get('WEB_HOST', WEB_HOST), WEB_PORT=get('WEB_PORT', web_port),
        WEB_SECRET=get('WEB_SECRET', WEB_SECRET), WEB_ALLOW_NETS=get('WEB_ALLOW_NETS', WEB_ALLOW_NETS),
        WEB_MQTT=flag('WEB_MQTT', WEB_MQTT), WEB_CONTROL=flag('WEB_CONTROL', WEB_CONTROL), SIM_MQTT=flag('SIM_MQTT', SIM_MQTT),
        STALE_SEC=get('STALE_SEC', STALE_SEC), METRICS_HOST=get('METRICS_HOST', METRICS_HOST), METRICS_PORT=get('METRICS_PORT', metrics_port),
        SIM_DB=get('SIM_DB', Path(state) / f'{site}-{device}-v1.db'),
        STATE_DB=get('STATE_DB', Path(state) / f'{role}-{site}.db'))
    # 이전 다중 시설 설정은 기존 검증기에서 명시적으로 거부하도록 전달한다.
    for key in ('SIM_SITES', 'A_SITE', 'B_SITE'):
        if key in env:
            result[key] = env[key]
    return result
