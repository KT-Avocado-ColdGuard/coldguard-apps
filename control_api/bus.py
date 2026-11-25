"""중앙 Mosquitto로 cmd를 발행한다. 구독은 하지 않는다.

- 명령 결과(ack)는 수집 워커가 command_ack 테이블에 저장하므로 API는 DB에서 읽는다.
  API가 직접 구독하면 재시작 중 받지 못한 ack가 사라지고, 파드마다 같은 ack를 중복으로 받는다.
- 발행만 하므로 영속 세션이 필요 없다(clean session). 클라이언트 ID는 파드마다 달라야 한다.
- 발행은 QoS 1이고, 브로커 수신 확인(PUBACK)을 기다린 뒤에 성공으로 본다.
"""
import json
import threading

from common.coldguard_runtime import log


class NotConnected(Exception):
    """브로커에 연결되어 있지 않아 발행하지 않았다(명령은 나가지 않았다)."""


class PublishUnconfirmed(Exception):
    """발행을 시도했지만 브로커 수신 확인을 받지 못했다. 재접속 후 나갈 수도 있다."""


class Publisher:
    def __init__(self, cfg, on_state=None, client=None):
        self._connected = threading.Event()
        self._on_state = on_state or (lambda up: None)
        if client is None:
            import paho.mqtt.client as mqtt
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg['MQTT_CLIENT_ID'],
                                 clean_session=True)
            client.username_pw_set(cfg['MQTT_USER'], cfg['MQTT_PASS'])
            if cfg['MQTT_TLS']:
                client.tls_set(ca_certs=cfg['MQTT_CA'])
            client.connect_timeout = 5
            client.reconnect_delay_set(1, 30)
        client.on_connect = self._connected_cb
        client.on_disconnect = self._disconnected_cb
        self._client = client
        self._addr = (cfg['MQTT_HOST'], cfg['MQTT_PORT'])
        self._client_id = cfg['MQTT_CLIENT_ID']

    def start(self):
        self._client.connect_async(*self._addr, keepalive=60)
        self._client.loop_start()

    def stop(self):
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def connected(self):
        return self._connected.is_set()

    def publish(self, topic, body, timeout=5.0):
        if not self.connected():
            raise NotConnected()
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False)
        info = self._client.publish(topic, payload, qos=1, retain=False)
        if info.rc != 0:  # MQTT_ERR_SUCCESS == 0. 연결이 막 끊긴 경우 등. 큐에 남아 재접속 후 나갈 수 있다.
            raise PublishUnconfirmed('rc=' + str(info.rc))
        try:
            info.wait_for_publish(timeout)
        except (RuntimeError, ValueError) as exc:
            raise PublishUnconfirmed(type(exc).__name__) from exc
        if not info.is_published():
            raise PublishUnconfirmed('timeout')

    def _connected_cb(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self._connected.clear()
            self._on_state(False)
        else:
            self._connected.set()
            self._on_state(True)
        log('control_api_mqtt_connect', reason=str(reason), client_id=self._client_id)

    def _disconnected_cb(self, client, userdata, flags, reason, properties):
        self._connected.clear()
        self._on_state(False)
        log('control_api_mqtt_disconnect', reason=str(reason), client_id=self._client_id)
