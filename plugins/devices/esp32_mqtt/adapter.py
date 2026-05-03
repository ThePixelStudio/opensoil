"""
ESP32 MQTT device adapter.
Implements IDevice — core only calls read() / write() / status().

Sensor readings arrive via MQTT subscriptions and are cached.
Actuator commands are published to MQTT topics.

Config:
  broker_host: localhost
  broker_port: 1883
  topic_prefix: garden
  node_id: node_left   # optional — enables multi-node topic namespacing
  username: (optional)
  password: (optional)

Single-node topics  (no node_id):
  opensoil/sensors/<sensor_id>
  opensoil/actuator/<actuator_id>/set

Multi-node topics   (node_id set):
  opensoil/nodes/<node_id>/sensors/<sensor_id>
  opensoil/nodes/<node_id>/actuator/<actuator_id>/set
"""

import json
import logging
import time
import threading
from typing import Any

import paho.mqtt.client as mqtt

from core.hardware.idevice import IDevice, SensorReadError, ActuatorWriteError

log = logging.getLogger("opensoil.device.esp32_mqtt")


class Adapter(IDevice):

    def __init__(self, config: dict):
        self._host    = config.get("broker_host", "localhost")
        self._port    = config.get("broker_port", 1883)
        self._prefix  = config.get("topic_prefix", "opensoil")
        self._node_id = config.get("node_id")  # None for single-node mode
        self._cache: dict[str, tuple[float, float]] = {}  # sensor_id -> (ts, value)
        self._connected = False
        self._start_ts  = time.time()
        self._lock      = threading.Lock()

        client_id = f"opensoil_ctrl_{self._node_id or 'main'}"
        self._client = mqtt.Client(client_id=client_id)
        if config.get("username"):
            self._client.username_pw_set(
                config["username"], config.get("password", "")
            )
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # connect_async schedules the connection without blocking so a broker
        # that isn't ready yet won't crash __init__. loop_start handles retries.
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

        # Wait up to 5s for connection
        deadline = time.time() + 5
        while not self._connected and time.time() < deadline:
            time.sleep(0.1)

        if not self._connected:
            log.warning(f"MQTT broker not reachable at {self._host}:{self._port} — will keep retrying")

    # ── IDevice interface ────────────────────────────────────────────────

    def read(self, sensor_id: str) -> float:
        with self._lock:
            entry = self._cache.get(sensor_id)
        if entry is None:
            raise SensorReadError(
                f"No data for sensor '{sensor_id}' — has ESP32 published yet?"
            )
        ts, value = entry
        age = time.time() - ts
        if age > 300:  # stale after 5 minutes
            raise SensorReadError(
                f"Sensor '{sensor_id}' data stale ({int(age)}s old)"
            )
        return value

    def write(self, actuator_id: str, value: Any) -> None:
        if self._node_id:
            topic = f"{self._prefix}/nodes/{self._node_id}/actuator/{actuator_id}/set"
        else:
            topic = f"{self._prefix}/actuator/{actuator_id}/set"
        payload = json.dumps({"value": value, "ts": time.time()})
        result = self._client.publish(topic, payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ActuatorWriteError(
                f"MQTT publish failed for {actuator_id}: rc={result.rc}"
            )
        log.debug(f"Actuator: {topic} -> {payload}")

    def status(self) -> dict:
        return {
            "connected":      self._connected,
            "transport":      "mqtt",
            "broker":         f"{self._host}:{self._port}",
            "node_id":        self._node_id,
            "uptime_sec":     int(time.time() - self._start_ts),
            "cached_sensors": list(self._cache.keys()),
        }

    # ── MQTT callbacks ───────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            if self._node_id:
                topic = f"{self._prefix}/nodes/{self._node_id}/sensors/#"
            else:
                topic = f"{self._prefix}/sensors/#"
            client.subscribe(topic, qos=0)
            label = f"node={self._node_id}" if self._node_id else "single-node"
            log.info(f"[{label}] Connected to MQTT broker, subscribed to '{topic}'")
        else:
            log.error(f"MQTT connect failed: rc={rc}")

    def _on_message(self, client, userdata, msg):
        """
        Single-node:  <prefix>/sensors/<sensor_id>
        Multi-node:   <prefix>/nodes/<node_id>/sensors/<sensor_id>
        Payload: float or {"value": float}
        """
        try:
            parts = msg.topic.split("/")
            sensor_id = parts[-1]
            payload = msg.payload.decode()
            try:
                data = json.loads(payload)
                value = float(data["value"] if isinstance(data, dict) else data)
            except (json.JSONDecodeError, KeyError):
                value = float(payload)
            with self._lock:
                self._cache[sensor_id] = (time.time(), value)
            log.debug(f"Sensor update: {sensor_id}={value}")
        except Exception as e:
            log.warning(f"Failed to parse MQTT message {msg.topic}: {e}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        label = f"node={self._node_id}" if self._node_id else "single-node"
        if rc == 7:
            log.warning(
                f"[{label}] MQTT disconnected (rc=7 — broker kicked old connection, "
                "probably because the NodeMCU reconnected with the same client_id). "
                "Auto-reconnect in progress."
            )
        else:
            log.warning(f"[{label}] MQTT disconnected (rc={rc}), will auto-reconnect")
