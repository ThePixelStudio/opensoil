"""
PluginLoader — discovers, validates, and registers all plugin types.
Inspired by OpenClaw's extension loader: scan folder → validate schema → register.

Plugin types:
  sensors/        sensor.yaml manifests
  actuators/      actuator.yaml manifests
  devices/        device.yaml + adapter.py
  domains/        domain.yaml + prompt.md + profiles/
  history_backends/ backend.yaml + backend.py
"""

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("opensoil.plugin_loader")

PLUGIN_DIR = Path(__file__).parent.parent / "plugins"


# ── Dataclasses for loaded plugins ──────────────────────────────────────────

@dataclass
class SensorPlugin:
    id: str
    name: str
    version: str
    author: str
    topics: list[dict]
    poll_interval_sec: int
    hardware: dict
    plugin_dir: Path

@dataclass
class ActuatorPlugin:
    id: str
    name: str
    version: str
    author: str
    commands: list[dict]
    safety: dict
    plugin_dir: Path

@dataclass
class DevicePlugin:
    id: str
    name: str
    version: str
    transport: str
    config_schema: dict
    supports: dict
    plugin_dir: Path
    adapter_class: Any = None   # loaded IDevice subclass

@dataclass
class DomainPlugin:
    id: str
    name: str
    version: str
    author: str
    sensors: list[dict]
    actuators: list[dict]
    context: list[dict]
    poll: dict
    safety: list[str]
    session: dict
    actuator_limits: dict        # per-actuator duration + no-effect rules
    prompt_template_path: Path
    profiles_dir: Path
    plugin_dir: Path

@dataclass
class HistoryBackendPlugin:
    id: str
    name: str
    plugin_dir: Path
    backend_class: Any = None

@dataclass
class ChatEnginePlugin:
    id: str
    name: str
    plugin_dir: Path
    engine_class: Any = None   # loaded IChatEngine subclass


# ── Required YAML fields per plugin type ────────────────────────────────────

SENSOR_REQUIRED   = {"id", "name", "version", "author", "topics", "poll_interval_sec"}
ACTUATOR_REQUIRED = {"id", "name", "version", "author", "commands"}
DEVICE_REQUIRED   = {"id", "name", "version", "transport"}
DOMAIN_REQUIRED   = {"id", "name", "version", "sensors", "actuators", "safety", "poll"}


# ── PluginLoader ─────────────────────────────────────────────────────────────

class PluginLoader:

    def __init__(self, plugin_dir: Path = PLUGIN_DIR):
        self.plugin_dir    = plugin_dir
        self.sensors:      dict[str, SensorPlugin]         = {}
        self.actuators:    dict[str, ActuatorPlugin]       = {}
        self.devices:      dict[str, DevicePlugin]         = {}
        self.domains:      dict[str, DomainPlugin]         = {}
        self.backends:     dict[str, HistoryBackendPlugin] = {}
        self.chat_engines: dict[str, ChatEnginePlugin]     = {}

    def load_all(self) -> None:
        """Scan plugin directory and load all plugin types."""
        self._load_sensors()
        self._load_actuators()
        self._load_devices()
        self._load_domains()
        self._load_history_backends()
        self._load_chat_engines()
        log.info(
            f"Loaded: {len(self.sensors)} sensors, {len(self.actuators)} actuators, "
            f"{len(self.devices)} devices, {len(self.domains)} domains, "
            f"{len(self.backends)} history backends, "
            f"{len(self.chat_engines)} chat engines"
        )

    # ── Sensors ─────────────────────────────────────────────────────────────

    def _load_sensors(self):
        base = self.plugin_dir / "sensors"
        if not base.exists():
            return
        for manifest in base.rglob("sensor.yaml"):
            try:
                data = self._load_yaml(manifest)
                self._validate(data, SENSOR_REQUIRED, manifest)
                plugin = SensorPlugin(
                    id=data["id"],
                    name=data["name"],
                    version=data["version"],
                    author=data["author"],
                    topics=data["topics"],
                    poll_interval_sec=data["poll_interval_sec"],
                    hardware=data.get("hardware", {}),
                    plugin_dir=manifest.parent,
                )
                self.sensors[plugin.id] = plugin
                log.debug(f"Sensor loaded: {plugin.id} v{plugin.version}")
            except Exception as e:
                log.error(f"Failed to load sensor plugin at {manifest}: {e}")

    # ── Actuators ───────────────────────────────────────────────────────────

    def _load_actuators(self):
        base = self.plugin_dir / "actuators"
        if not base.exists():
            return
        for manifest in base.rglob("actuator.yaml"):
            try:
                data = self._load_yaml(manifest)
                self._validate(data, ACTUATOR_REQUIRED, manifest)
                plugin = ActuatorPlugin(
                    id=data["id"],
                    name=data["name"],
                    version=data["version"],
                    author=data["author"],
                    commands=data["commands"],
                    safety=data.get("safety", {}),
                    plugin_dir=manifest.parent,
                )
                self.actuators[plugin.id] = plugin
                log.debug(f"Actuator loaded: {plugin.id} v{plugin.version}")
            except Exception as e:
                log.error(f"Failed to load actuator plugin at {manifest}: {e}")

    # ── Devices ─────────────────────────────────────────────────────────────

    def _load_devices(self):
        base = self.plugin_dir / "devices"
        if not base.exists():
            return
        for manifest in base.rglob("device.yaml"):
            try:
                data = self._load_yaml(manifest)
                self._validate(data, DEVICE_REQUIRED, manifest)
                plugin = DevicePlugin(
                    id=data["id"],
                    name=data["name"],
                    version=data["version"],
                    transport=data["transport"],
                    config_schema=data.get("config_schema", {}),
                    supports=data.get("supports", {}),
                    plugin_dir=manifest.parent,
                )
                # Load adapter class from adapter.py if present
                adapter_path = manifest.parent / "adapter.py"
                if adapter_path.exists():
                    plugin.adapter_class = self._load_class(
                        adapter_path, "Adapter"
                    )
                self.devices[plugin.id] = plugin
                log.debug(f"Device loaded: {plugin.id} ({plugin.transport})")
            except Exception as e:
                log.error(f"Failed to load device plugin at {manifest}: {e}")

    # ── Domains ─────────────────────────────────────────────────────────────

    def _load_domains(self):
        base = self.plugin_dir / "domains"
        if not base.exists():
            return
        for manifest in base.rglob("domain.yaml"):
            try:
                data = self._load_yaml(manifest)
                self._validate(data, DOMAIN_REQUIRED, manifest)
                prompt_path = manifest.parent / "prompts" / "base.md"
                profiles_dir = manifest.parent / "profiles"
                plugin = DomainPlugin(
                    id=data["id"],
                    name=data["name"],
                    version=data.get("version", "1.0.0"),
                    author=data.get("author", "unknown"),
                    sensors=data["sensors"],
                    actuators=data["actuators"],
                    context=data.get("context", []),
                    poll=data["poll"],
                    safety=data["safety"],
                    session=data.get("session", {}),
                    actuator_limits=data.get("actuator_limits", {}),
                    prompt_template_path=prompt_path,
                    profiles_dir=profiles_dir,
                    plugin_dir=manifest.parent,
                )
                self.domains[plugin.id] = plugin
                log.debug(f"Domain loaded: {plugin.id}")
            except Exception as e:
                log.error(f"Failed to load domain plugin at {manifest}: {e}")

    # ── History backends ─────────────────────────────────────────────────────

    def _load_history_backends(self):
        base = self.plugin_dir / "history_backends"
        if not base.exists():
            return
        for manifest in base.rglob("backend.yaml"):
            try:
                data = self._load_yaml(manifest)
                plugin = HistoryBackendPlugin(
                    id=data["id"],
                    name=data["name"],
                    plugin_dir=manifest.parent,
                )
                backend_path = manifest.parent / "backend.py"
                if backend_path.exists():
                    plugin.backend_class = self._load_class(backend_path, "Backend")
                self.backends[plugin.id] = plugin
                log.debug(f"History backend loaded: {plugin.id}")
            except Exception as e:
                log.error(f"Failed to load history backend at {manifest}: {e}")

    # ── Chat engines ─────────────────────────────────────────────────────────

    def _load_chat_engines(self):
        base = self.plugin_dir / "chat_engines"
        if not base.exists():
            return
        for manifest in base.rglob("chat_engine.yaml"):
            try:
                data   = self._load_yaml(manifest)
                plugin = ChatEnginePlugin(
                    id=data["id"],
                    name=data["name"],
                    plugin_dir=manifest.parent,
                )
                engine_path = manifest.parent / "engine.py"
                if engine_path.exists():
                    plugin.engine_class = self._load_class(engine_path, "Engine")
                self.chat_engines[plugin.id] = plugin
                log.debug(f"Chat engine loaded: {plugin.id}")
            except Exception as e:
                log.error(f"Failed to load chat engine at {manifest}: {e}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _load_yaml(self, path: Path) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)

    def _validate(self, data: dict, required: set, path: Path):
        missing = required - data.keys()
        if missing:
            raise ValueError(
                f"Plugin manifest {path} missing required fields: {missing}"
            )

    def _load_class(self, path: Path, class_name: str) -> Any:
        # Build a stable dotted module name from the path so the module is
        # registered in sys.modules and absolute imports inside the file work.
        app_root = Path(__file__).parent.parent
        try:
            rel = path.relative_to(app_root)
            module_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        except ValueError:
            module_name = path.stem

        if module_name in sys.modules:
            return getattr(sys.modules[module_name], class_name)

        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return getattr(module, class_name)

    # ── Public helpers ───────────────────────────────────────────────────────

    def get_sensor(self, sensor_id: str) -> SensorPlugin:
        if sensor_id not in self.sensors:
            raise KeyError(f"Sensor plugin '{sensor_id}' not found")
        return self.sensors[sensor_id]

    def get_actuator(self, actuator_id: str) -> ActuatorPlugin:
        if actuator_id not in self.actuators:
            raise KeyError(f"Actuator plugin '{actuator_id}' not found")
        return self.actuators[actuator_id]

    def get_domain(self, domain_id: str) -> DomainPlugin:
        if domain_id not in self.domains:
            raise KeyError(f"Domain plugin '{domain_id}' not found")
        return self.domains[domain_id]

    def get_device(self, device_id: str) -> DevicePlugin:
        if device_id not in self.devices:
            raise KeyError(f"Device plugin '{device_id}' not found")
        return self.devices[device_id]
