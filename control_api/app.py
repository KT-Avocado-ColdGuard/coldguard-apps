from flask import Flask, jsonify

from control_api import config
from control_api.bus import MqttBus

app = Flask(__name__)
app.config["BUS"] = None


@app.get("/healthz")
def healthz():
    bus = app.config["BUS"]
    ok = bus is not None and bus.connected()
    return jsonify({"status": "ok" if ok else "mqtt_down", "mqtt": ok}), (200 if ok else 503)


def on_ack(topic, ack):
    print("ACK", topic, ack, flush=True)


def main():
    cfg = config.load()
    bus = MqttBus(cfg, on_ack)
    app.config["BUS"] = bus
    bus.start()
    try:
        app.run(host="127.0.0.1", port=cfg["PORT"])
    finally:
        bus.stop()


if __name__ == "__main__":
    main()
