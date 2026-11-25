"""중앙(지금은 시설) Mosquitto 연결: TLS, ack 구독."""
import json
import threading

import paho.mqtt.client as mqtt

ACK_TOPIC = "coldguard/+/+/ack"


class MqttBus:
    def __init__(self, cfg, on_ack):
        self._on_ack = on_ack
        self._connected = threading.Event()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["MQTT_CLIENT_ID"])
        c.username_pw_set(cfg["MQTT_USER"], cfg["MQTT_PASS"])
        c.tls_set(ca_certs=cfg["MQTT_CA"])
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._client = c
        self._addr = (cfg["MQTT_HOST"], cfg["MQTT_PORT"])

    def start(self):
        self._client.connect_async(*self._addr, keepalive=60)
        self._client.loop_start()

    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()

    def connected(self):
        return self._connected.is_set()

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            print("mqtt_connect_failed", reason, flush=True)
            return
        client.subscribe(ACK_TOPIC, qos=1)
        self._connected.set()
        print("mqtt_connect", reason, flush=True)

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        self._connected.clear()
        print("mqtt_disconnect", reason, flush=True)

    def _on_message(self, client, userdata, msg):
        try:
            self._on_ack(msg.topic, json.loads(msg.payload))
        except ValueError:
            print("ack_invalid_json", msg.topic, flush=True)
