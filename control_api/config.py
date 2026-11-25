"""설정: 환경변수로만 받는다 (비밀번호를 코드에 쓰지 않음)."""
import os

REQUIRED = ("MQTT_HOST", "MQTT_CA", "MQTT_USER", "MQTT_PASS")


def load(env=None):
    env = os.environ if env is None else env
    cfg = {
        "MQTT_HOST": env.get("MQTT_HOST", ""),
        "MQTT_PORT": int(env.get("MQTT_PORT", "8883")),
        "MQTT_CA": env.get("MQTT_CA", ""),
        "MQTT_USER": env.get("MQTT_USER", ""),
        "MQTT_PASS": env.get("MQTT_PASS", ""),
        "MQTT_CLIENT_ID": env.get("MQTT_CLIENT_ID", "control-api"),
        "PORT": int(env.get("CONTROL_API_PORT", "8000")),
    }
    missing = [k for k in REQUIRED if not cfg[k]]
    if missing:
        raise ValueError("설정 누락: " + ", ".join(missing))
    return cfg
