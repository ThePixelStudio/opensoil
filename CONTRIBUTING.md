# Contributing to OpenSoil

OpenSoil is **plugin-first**. The core runtime never changes — all domain
knowledge, hardware support, and grow profiles live in `plugins/`.

> **Rule #1:** Never modify `core/` to add a sensor, actuator, domain, or
> hardware adapter. Add a plugin instead. Pull requests that touch `core/`
> without a core bug fix will be returned.

---

## Table of contents

1. [Plugin types at a glance](#1-plugin-types-at-a-glance)
2. [Development setup](#2-development-setup)
3. [Sensor plugin](#3-sensor-plugin)
4. [Actuator plugin](#4-actuator-plugin)
5. [Plant profile](#5-plant-profile)
6. [Domain plugin](#6-domain-plugin)
7. [Device adapter plugin](#7-device-adapter-plugin)
8. [History backend plugin](#8-history-backend-plugin)
9. [Testing your plugin](#9-testing-your-plugin)
10. [Submitting a PR](#10-submitting-a-pr)

---

## 1. Plugin types at a glance

| Type | Folder | Min files | Code needed? |
|---|---|---|---|
| Sensor | `plugins/sensors/<id>/` | `sensor.yaml` | No |
| Actuator | `plugins/actuators/<id>/` | `actuator.yaml` | No |
| Plant profile | `plugins/domains/<domain>/profiles/<id>.yaml` | one YAML file | No |
| Domain | `plugins/domains/<id>/` | `domain.yaml` + `prompts/base.md` | No |
| Device adapter | `plugins/devices/<id>/` | `device.yaml` + `adapter.py` | Yes — 3 methods |
| History backend | `plugins/history_backends/<id>/` | `backend.yaml` + `backend.py` | Yes — implements `IHistoryStore` |

The plugin loader scans all folders at startup, validates YAML schemas, and
registers plugins automatically. A bad plugin is rejected with a clear error —
it never crashes the runtime.

---

## 2. Development setup

```bash
git clone https://github.com/opensoil/opensoil
cd opensoil
pip install -e ".[dev]"       # installs the package + dev tools

# Verify everything loads
opensoil plugins
opensoil validate --config config.yaml
```

Branch naming:
```
plugin/bh1750-light-sensor
plugin/aquarium-domain
fix/sqlite-trend-edge-case
```

---

## 3. Sensor plugin

A sensor plugin tells OpenSoil what MQTT topic to listen on, what unit the
value is in, and what hardware it represents. No Python needed.

**Create `plugins/sensors/bh1750/sensor.yaml`:**

```yaml
id: bh1750
name: BH1750 Light Sensor
version: 1.0.0
author: yourname
description: I2C ambient light sensor, 1–65535 lux resolution

topics:
  - key: bh1750_lux
    name: Light intensity
    unit: lux
    mqtt: sensors/bh1750_lux   # must match what the firmware publishes
    range: [0, 65535]

poll_interval_sec: 60

hardware:
  protocol: i2c
  address: "0x23"              # ADDR pin LOW = 0x23, HIGH = 0x5C
  esphome_platform: bh1750     # ESPHome component name if applicable

compatible_devices:
  - esp32_mqtt
  - rpi_gpio
```

**That's it.** Once this file exists, the sensor ID `bh1750_lux` is available
in any `domain.yaml` sensors list and in plant profile targets.

### Firmware side

The firmware (ESP8266/ESP32) must publish a float to the matching MQTT topic:

```
opensoil/sensors/bh1750_lux   →   "1420.0"
```

The `topic_prefix` (default `opensoil`) comes from `config.yaml`.

---

## 4. Actuator plugin

Same pattern as sensors — just YAML, no Python.

**Create `plugins/actuators/relay_humidifier/actuator.yaml`:**

```yaml
id: relay_humidifier
name: Humidifier relay
version: 1.0.0
author: yourname
description: Relay-controlled ultrasonic humidifier (active-LOW)

type: relay                    # relay | pwm | servo | digital
commands:
  - "on"
  - "off"

hardware:
  signal: digital
  active_low: true             # LOW = ON, HIGH = OFF

compatible_devices:
  - esp32_mqtt
  - rpi_gpio
```

The device adapter receives `device.write("relay_humidifier", "on")` and
publishes to `opensoil/actuator/relay_humidifier/set`. The firmware subscribes
and drives the pin.

---

## 5. Plant profile

A profile defines grow phases, sensor targets, and notes for a specific
plant variety. It lives inside a domain's `profiles/` folder and requires
zero code.

**Create `plugins/domains/smart_garden/profiles/basil.yaml`:**

```yaml
id: basil
name: Basil
cultivar: Genovese
harvest_days: 30
expected_duration_days: 90
notes: >
  Warm-season herb. Very sensitive to cold — never below 15 °C.
  Pinch flower buds to extend leaf production. Needs high light.

phases:
  germination:
    days: [0, 7]
    targets:
      dht11_temp:     [22, 28]
      dht11_humidity: [70, 85]
      soil_resistive: [68, 78]
    notes: Keep soil consistently moist. No direct light yet.

  seedling:
    days: [7, 21]
    targets:
      dht11_temp:     [20, 26]
      dht11_humidity: [60, 75]
      soil_resistive: [58, 70]
    notes: Introduce light gradually. Watch for damping-off.

  vegetative:
    days: [21, 90]
    targets:
      dht11_temp:     [20, 28]
      dht11_humidity: [50, 70]
      soil_resistive: [50, 65]
    notes: High light (70–90 % PWM). Pinch tips to encourage bushiness.
```

Use it with `profile: basil` in `config.yaml`. The web UI profile tab and
the LLM context assembler both pick it up automatically.

**Sensor IDs must match what your device publishes.** Use `opensoil sensors`
to see the exact IDs active in your box.

---

## 6. Domain plugin

A domain defines an entire IoT use case: which sensors and actuators are
involved, what safety rules apply, and what the LLM should know. Use the
smart garden as a reference.

**Minimum structure:**

```
plugins/domains/aquarium/
├── domain.yaml
├── prompts/
│   └── base.md
└── profiles/
    └── tropical_fish.yaml   (optional)
```

**`domain.yaml` skeleton:**

```yaml
id: aquarium
name: Aquarium Controller
version: 1.0.0
author: yourname
description: >
  LLM-driven aquarium management. Controls heater, filter, and lighting.

sensors:
  - id: ds18b20_water_temp
    name: Water temperature
    unit: celsius
    required: true
  - id: tds_ppm
    name: TDS / nutrient level
    unit: ppm
    required: false

actuators:
  - id: relay_heater
    name: Water heater
  - id: relay_filter
    name: Circulation filter
  - id: pwm_light
    name: Aquarium light (PWM)

safety:
  # Safety rules are MANDATORY for any domain with actuators.
  # Syntax: <actuator> ON|OFF [if <sensor> <op> <value>] [between HH:MM and HH:MM]
  - "relay_heater OFF if ds18b20_water_temp > 30"
  - "relay_heater ON (forced) if ds18b20_water_temp < 22"
  - "pwm_light OFF between 22:00 and 07:00"

poll:
  interval_sec: 60

session:
  concept: tank_cycle          # domain-defined lifecycle concept
  start_event: TANK_STARTED
  end_event: TANK_RESET
```

**`prompts/base.md` — LLM instructions:**

Write this as if briefing an expert. Tell the LLM what it is, what it
controls, and the reasoning principles specific to your domain. The smart
garden `prompts/base.md` is a good template to follow.

```markdown
# Aquarium Controller — Instructions

You are an expert aquarist managing a tropical freshwater tank.
You run autonomously every 60 seconds.

## Your role
...
```

**Safety rules are required** for any domain that has actuators. A PR
without safety rules for actuators will not be merged.

---

## 7. Device adapter plugin

A device adapter bridges OpenSoil to a specific hardware transport. The
interface is exactly 3 methods: `read`, `write`, `status`.

**`plugins/devices/rpi_gpio/device.yaml`:**

```yaml
id: rpi_gpio
name: Raspberry Pi GPIO (direct)
version: 1.0.0
author: yourname
transport: gpio
description: Read sensors and write actuators via Pi GPIO pins directly.
adapter_class: Adapter
```

**`plugins/devices/rpi_gpio/adapter.py`:**

```python
from core.hardware.idevice import IDevice, SensorReadError, ActuatorWriteError

class Adapter(IDevice):

    def __init__(self, config: dict):
        # config comes from config.yaml → device.config
        self._pin_map = config.get("pin_map", {})
        # e.g. pin_map: {dht11_temp: 4, relay_pump: 17}

    def read(self, sensor_id: str) -> float:
        pin = self._pin_map.get(sensor_id)
        if pin is None:
            raise SensorReadError(f"No GPIO pin mapped for sensor '{sensor_id}'")
        # ... read the pin and return float
        return value

    def write(self, actuator_id: str, value) -> None:
        pin = self._pin_map.get(actuator_id)
        if pin is None:
            raise ActuatorWriteError(f"No GPIO pin mapped for actuator '{actuator_id}'")
        # ... drive the pin

    def status(self) -> dict:
        return {"connected": True, "transport": "gpio"}
```

**Rules:**
- The class **must** be named `Adapter`
- `read()` must return a `float`
- Raise `SensorReadError` / `ActuatorWriteError` (from `core.hardware.idevice`)
  on failure — never return `None`
- No core imports beyond `core.hardware.idevice`
- No side effects in `__init__` that block for more than 5 seconds

---

## 8. History backend plugin

A history backend stores sensor readings, events, and LLM decisions.
The default is SQLite — contribute alternatives (InfluxDB, TimescaleDB, etc.).

**`plugins/history_backends/influxdb/backend.yaml`:**

```yaml
id: influxdb
name: InfluxDB backend
version: 1.0.0
author: yourname
description: Time-series backend for Grafana dashboards.
backend_class: InfluxDBBackend
```

**`plugins/history_backends/influxdb/backend.py`** — implement all methods
of `IHistoryStore` from `core/history/ihistory_store.py`:

```python
from core.history.ihistory_store import (
    IHistoryStore, SensorReading, Event, LLMDecision, Session
)

class InfluxDBBackend(IHistoryStore):

    def __init__(self, box_id: str, config: dict):
        self.box_id = box_id
        # config comes from config.yaml → history.config
        ...

    def write_reading(self, r: SensorReading) -> None: ...
    def write_event(self, e: Event) -> None: ...
    def write_decision(self, d: LLMDecision) -> None: ...
    def sensor_avg(self, box_id: str, window: int) -> dict: ...
    def sensor_minmax(self, box_id: str, window: int) -> dict: ...
    def sensor_trend(self, box_id: str, window: int) -> dict: ...
    def events(self, box_id: str, **kwargs) -> list: ...
    def llm_decisions(self, box_id: str, limit: int = 5) -> list: ...
    def create_session(self, session: Session) -> str: ...
    def close_session(self, session_id: str, outcome: str, notes=None) -> None: ...
```

Enable it in `config.yaml`:
```yaml
history:
  backend: influxdb
  config:
    host: localhost
    port: 8086
    token: your-token
    bucket: opensoil
```

---

## 9. Testing your plugin

```bash
# 1. Confirm it loads without errors
opensoil plugins

# 2. Validate config + plugin schema
opensoil validate --config config.yaml

# 3. Run the test suite
pytest tests/

# 4. For device adapters — run integration test with a mock device
pytest tests/integration/

# 5. Lint
ruff check plugins/your_plugin_folder/
```

For sensor and actuator YAML-only plugins, steps 1 and 2 are sufficient.
For device adapters and history backends, the integration tests must pass.

---

## 10. Submitting a PR

### Checklist

- [ ] Plugin lives entirely in `plugins/` — no `core/` changes
- [ ] YAML has `id`, `name`, `version`, `author` fields
- [ ] Sensor/actuator IDs are lowercase with underscores (`bh1750_lux`, not `BH1750Lux`)
- [ ] Domain has `safety:` rules for every actuator
- [ ] Plant profile sensor IDs match an existing or co-submitted sensor plugin
- [ ] `opensoil plugins` and `opensoil validate` pass cleanly
- [ ] `pytest tests/` passes
- [ ] PR description includes a `config.yaml` snippet showing how to use the plugin
- [ ] No secrets, API keys, or personal credentials committed

### PR description template

```
## Plugin: <id>

**Type:** sensor | actuator | domain | device | history backend | profile

**What it does:**
One paragraph.

**Hardware / dependencies:**
e.g. BH1750 I2C module, python-influxdb-client>=1.40

**config.yaml snippet:**
```yaml
sensors:
  - plugin: bh1750
```

**Tested on:**
e.g. ESP8266 NodeMCU + Mosquitto 2.0, Raspberry Pi 3B
```

### What reviewers check

| Area | What we look at |
|---|---|
| Safety | Actuator plugins need safety rules in their reference domain |
| Schema | All required YAML fields present and correctly typed |
| Core isolation | No imports from `core/` except `IDevice` / `IHistoryStore` |
| Sensor IDs | Lowercase underscore, globally unique, descriptive |
| Backwards compat | New plugins don't break existing `config.yaml` files |

---

## Questions?

Open a [GitHub Discussion](https://github.com/opensoil/opensoil/discussions)
or file an issue with the `question` label before starting a large plugin —
we can confirm the approach before you invest the time.
