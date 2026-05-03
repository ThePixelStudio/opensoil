"""
DeviceRouter — aggregates reads across multiple IDevice nodes.

When config.yaml uses `nodes:` instead of `device:`, the orchestrator
creates one adapter per node and wraps them in this router.

Read behaviour:
  - Each node publishes sensors under namespaced IDs:
      {node_id}_{sensor_id}   e.g. node_left_dht11_temp
  - Aggregate virtual IDs are computed across all nodes that have the sensor:
      {sensor_id}             average (e.g. dht11_temp)
      {sensor_id}_min         minimum
      {sensor_id}_max         maximum
  - If only one node has a sensor, its value IS the aggregate — no suffix needed.

Write behaviour:
  - Actuator IDs are namespaced in node config:
      actuators: [relay_pump]   →  node owns relay_pump
  - Router dispatches write(actuator_id, value) to the owning node.
  - Unknown actuator_id → tries all nodes, logs warning.

Partial failure:
  - If a node is offline its sensors are silently skipped.
  - As long as at least one node provides data, the poll continues.
  - A node returning no readings increments its miss counter — logged at WARN.
"""

import logging
from typing import Any

from core.hardware.idevice import IDevice, SensorReadError, ActuatorWriteError

log = logging.getLogger("opensoil.device_router")


class DeviceRouter(IDevice):
    """
    Wraps N IDevice adapters and presents them as a single IDevice to the
    orchestrator. Node adapters are passed in as (node_id, adapter, actuator_ids).
    """

    def __init__(self, nodes: list[tuple[str, IDevice, list[str]]]):
        """
        nodes: list of (node_id, adapter_instance, [actuator_id, ...])
        """
        self._nodes: list[tuple[str, IDevice, list[str]]] = nodes
        self._miss_counts: dict[str, int] = {nid: 0 for nid, _, _ in nodes}

    # ── IDevice interface ────────────────────────────────────────────────────

    def read(self, sensor_id: str) -> float:
        """
        If sensor_id has a node prefix (node_left_dht11_temp), read from that node.
        If sensor_id ends with _min or _max, return aggregate.
        Otherwise return aggregate average across all nodes that have the sensor.
        """
        # Namespaced read: node_left_dht11_temp
        for node_id, adapter, _ in self._nodes:
            prefix = f"{node_id}_"
            if sensor_id.startswith(prefix):
                bare = sensor_id[len(prefix):]
                return adapter.read(bare)

        # Aggregate _min / _max
        if sensor_id.endswith("_min"):
            return self._aggregate(sensor_id[:-4], "min")
        if sensor_id.endswith("_max"):
            return self._aggregate(sensor_id[:-4], "max")

        # Default: aggregate average
        return self._aggregate(sensor_id, "avg")

    def write(self, actuator_id: str, value: Any) -> None:
        """Route write to the node that owns this actuator."""
        for node_id, adapter, actuator_ids in self._nodes:
            if actuator_id in actuator_ids:
                log.debug(f"Routing write {actuator_id}={value} → node {node_id}")
                adapter.write(actuator_id, value)
                return
        # Fallback: broadcast to all nodes, ignore individual failures
        log.warning(f"Actuator '{actuator_id}' not assigned to any node — broadcasting")
        errors = []
        for node_id, adapter, _ in self._nodes:
            try:
                adapter.write(actuator_id, value)
            except ActuatorWriteError as e:
                errors.append(f"{node_id}: {e}")
        if len(errors) == len(self._nodes):
            raise ActuatorWriteError(
                f"Write {actuator_id}={value} failed on all nodes: {errors}"
            )

    def status(self) -> dict:
        return {
            "type": "multi_node",
            "nodes": [
                {
                    "id":     node_id,
                    "status": adapter.status(),
                    "misses": self._miss_counts.get(node_id, 0),
                }
                for node_id, adapter, _ in self._nodes
            ],
        }

    # ── Snapshot helper (used by orchestrator) ───────────────────────────────

    def read_snapshot(self, sensor_ids: list[str]) -> dict[str, float]:
        """
        Read all sensors across all nodes. Returns a dict with:
          - {node_id}_{sensor_id} for each node-specific reading
          - {sensor_id}           aggregate average
          - {sensor_id}_min       aggregate min
          - {sensor_id}_max       aggregate max

        Sensors that fail on every node are omitted (partial failure tolerance).
        """
        # Collect per-node readings: {sensor_id: {node_id: value}}
        per_sensor: dict[str, dict[str, float]] = {}

        for node_id, adapter, _ in self._nodes:
            node_readings = {}
            for sid in sensor_ids:
                try:
                    val = adapter.read(sid)
                    node_readings[sid] = val
                except SensorReadError:
                    pass
                except Exception as e:
                    log.warning(f"Node {node_id} read error for {sid}: {e}")

            if node_readings:
                self._miss_counts[node_id] = 0
            else:
                self._miss_counts[node_id] += 1
                if self._miss_counts[node_id] == 1:
                    log.warning(f"Node {node_id} returned no readings (may be offline)")

            for sid, val in node_readings.items():
                per_sensor.setdefault(sid, {})[node_id] = val

        if not per_sensor:
            return {}

        result: dict[str, float] = {}

        for sid, node_vals in per_sensor.items():
            values = list(node_vals.values())

            # Node-specific IDs
            for node_id, val in node_vals.items():
                result[f"{node_id}_{sid}"] = val

            # Aggregates (only emit _min/_max when >1 node has the sensor)
            avg = sum(values) / len(values)
            result[sid] = round(avg, 2)
            if len(values) > 1:
                result[f"{sid}_min"] = round(min(values), 2)
                result[f"{sid}_max"] = round(max(values), 2)

        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _aggregate(self, sensor_id: str, mode: str) -> float:
        values = []
        for node_id, adapter, _ in self._nodes:
            try:
                values.append(adapter.read(sensor_id))
            except (SensorReadError, Exception):
                pass
        if not values:
            raise SensorReadError(
                f"No node could read sensor '{sensor_id}'"
            )
        if mode == "min":
            return min(values)
        if mode == "max":
            return max(values)
        return sum(values) / len(values)
