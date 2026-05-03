# Plugin Development Guide

Complete reference for writing all five OpenSoil plugin types.
No core code modifications needed for any plugin.

---

## Quick reference

```
plugins/
├── sensors/<id>/           sensor.yaml [+ driver.py]
├── actuators/<id>/         actuator.yaml [+ driver.py]
├── devices/<id>/           device.yaml + adapter.py
├── domains/<id>/           domain.yaml + prompts/base.md [+ profiles/*.yaml]
└── history_backends/<id>/  backend.yaml + backend.py
```

The plugin loader scans these folders at startup, validates schemas,
and registers plugins. Bad YAML = clear error at startup, not runtime crash.

---

## 1. Sensor plugin

Minimum: `sensor.yaml` only (no Python needed for most sensors).

### sensor.yaml schema

```yaml
id: my_sensor                    # unique, snake_case, matches MQTT key
name: My Sensor Display Name
version: 1.0.0
author: yourname

topics:                          # one entry per reading this sensor produces
  - key: my_temp                 # sensor_id used in device.read(sensor_id)
    name: Temperature            # human-readable
    unit: celsius                # celsius | percent | lux | ppm | bool | watts | ...
    mqtt: sensors/my_temp        # MQTT sub-topic (after topic_prefix)
    range: [-40, 125]            # optional: for UI validation

poll_interval_sec: 30            # how often ESP32 should publish (reference only)

hardware:
  protocol: i2c                  # i2c | gpio | adc | uart | spi
  address: "0x44"                # for I2C sensors
  esphome_platform: sht3xd       # ESPHome platform name (for firmware gen)

compatible_devices:              # optional: which device adapters work
  - esp32_mqtt
  - rpi_gpio
```

### When you need driver.py

Only if the sensor needs custom preprocessing before the value is usable.
Examples: CO2 sensor with temperature compensation, pH sensor with calibration curve.

```python
# plugins/sensors/my_sensor/driver.py
class Driver:
    def __init__(self, config: dict):
        self.calibration = config.get("calibration", {})

    def process(self, raw_value: float) -> float:
        """Transform raw ADC reading to calibrated value."""
        return raw_value * self.calibration.get("factor", 1.0)
```

---

## 2. Actuator plugin

Minimum: `actuator.yaml` only.

### actuator.yaml schema

```yaml
id: my_actuator
name: My Actuator Display Name
version: 1.0.0
author: yourname

commands:
  - key: my_actuator             # actuator_id used in device.write(actuator_id, val)
    name: On/off control
    type: on_off                 # on_off | integer | float | pwm
    mqtt: actuator/my_actuator/set
    values: ["on", "off"]        # for on_off type
    # range: [0, 100]            # for integer/float/pwm type
    # unit: percent              # for pwm/float

safety:                          # optional — enforced by SafetyEngine
  interlock:
    sensor: soil_cap             # sensor_id to check
    condition: "< 78"            # condition for actuation to be ALLOWED
    message: "Pump blocked — soil too wet"
  max_duration_sec: 120          # auto-off after N seconds (for pumps)
  cooldown_sec: 300              # min seconds between activations

hardware:
  gpio_type: relay               # relay | ledc_pwm | servo | dac
  voltage: [5, 12]               # supported voltages
  typical_gpio: GPIO26           # example wiring
```

---

## 3. Device adapter plugin

Requires: `device.yaml` + `adapter.py` (must implement `IDevice`).

### device.yaml schema

```yaml
id: my_device
name: My Device Name
version: 1.0.0
transport: mqtt                  # mqtt | serial | gpio | http | zigbee | custom

config_schema:                   # documents what config.yaml device.config accepts
  my_setting:
    type: string
    default: localhost
    description: What this setting does

supports:
  protocols: [i2c, gpio]
  sensors: [sht31, bh1750]       # which sensor plugins work with this device
  actuators: [relay_pump]
```

### adapter.py — implement exactly 3 methods

```python
from core.hardware.idevice import IDevice, SensorReadError, ActuatorWriteError

class Adapter(IDevice):
    """Class MUST be named 'Adapter'. Loader finds it by this name."""

    def __init__(self, config: dict):
        """
        config = device.config section from config.yaml
        e.g. {"broker_host": "192.168.1.100", "broker_port": 1883}
        """
        # setup your connection here
        pass

    def read(self, sensor_id: str) -> float:
        """
        Return current sensor value.
        Raise SensorReadError if sensor unavailable or data stale.
        """
        raise SensorReadError(f"Sensor {sensor_id} not available")

    def write(self, actuator_id: str, value) -> None:
        """
        Send command to actuator.
        value: "on"|"off" for relays, 0-100 for PWM, etc.
        Raise ActuatorWriteError if device unreachable.
        """
        raise ActuatorWriteError(f"Could not write to {actuator_id}")

    def status(self) -> dict:
        """
        Must include: connected (bool), transport (str), uptime_sec (int).
        """
        return {
            "connected":  True,
            "transport":  "my_transport",
            "uptime_sec": 0,
        }
```

### Reference implementation

See `plugins/devices/esp32_mqtt/adapter.py` — full MQTT subscriber/publisher
with connection management, value caching, and staleness detection.

---

## 4. Domain plugin

Requires: `domain.yaml` + `prompts/base.md`.
Optional: `profiles/<plant_or_config>.yaml` for sub-profiles.

### domain.yaml schema

```yaml
id: my_domain
name: My Domain Name
version: 1.0.0
author: yourname
description: What this domain does

sensors:                         # what sensors this domain uses
  - id: my_sensor_id             # must match sensor plugin id
    name: Human readable name
    unit: celsius
    required: true               # if true: error at startup if not connected

actuators:                       # what actuators this domain controls
  - id: my_actuator_id
    name: Human readable name

context:                         # extra variables injected into LLM context
  - key: time_of_day
    source: system_clock
    format: HH:MM
  - key: day_number
    source: session

poll:
  interval_sec: 60               # how often to call LLM
  triggers:                      # also poll immediately on these events
    - sensor: my_sensor_id
      condition: "> 50"
      reason: threshold_alert

safety:                          # REQUIRED — hard constraints
  - "my_actuator OFF if my_sensor > 100"
  - "my_actuator ON (forced) if my_sensor < 5"
  - "my_actuator OFF between 22:00 and 06:00"

session:
  concept: grow_cycle            # what a "session" means for this domain
  start_event: START_EVENT_NAME
  end_event: END_EVENT_NAME
```

### prompts/base.md — LLM instructions for this domain

Written in Markdown. Injected into the SYSTEM message after domain identity,
sensors, actuators, safety rules, and JSON schema.

```markdown
# My Domain — Controller Instructions

You are an expert in [domain topic].

## Decision principles
1. [Principle 1]
2. [Principle 2]

## Output format
Respond ONLY in valid JSON. No preamble.
The "reason" field is logged permanently — be specific.
```

### profiles/<name>.yaml — sub-profiles (optional)

Used when a domain has multiple variants (plant species, room types, etc.)

```yaml
id: my_profile
name: My Profile Name
expected_duration_days: 60

phases:
  phase_one:
    days: [0, 20]
    targets:
      sensor_id_1: [min, max]
      sensor_id_2: [min, max]
    notes: Optional notes about this phase

  phase_two:
    days: [20, 60]
    targets:
      sensor_id_1: [min, max]
```

---

## 5. History backend plugin

Requires: `backend.yaml` + `backend.py` (must implement `IHistoryStore`).

### backend.yaml

```yaml
id: my_backend
name: My History Backend
description: Store sensor history in [your system]
```

### backend.py — implement IHistoryStore

```python
from core.history.ihistory_store import (
    IHistoryStore, SensorReading, Event, LLMDecision, Session
)

class Backend(IHistoryStore):
    """Class MUST be named 'Backend'."""

    def __init__(self, box_id: str, config: dict):
        """config = history.config section from config.yaml"""
        pass

    def write_reading(self, reading: SensorReading) -> None: ...
    def write_event(self, event: Event) -> None: ...
    def write_decision(self, decision: LLMDecision) -> None: ...
    def sensor_avg(self, box_id: str, window: int) -> dict: ...
    def sensor_minmax(self, box_id: str, window: int) -> dict: ...
    def sensor_trend(self, box_id: str, window: int) -> dict: ...
    def events(self, box_id, type=None, since=None) -> list: ...
    def llm_decisions(self, box_id, limit=5) -> list: ...
    def create_session(self, session: Session) -> str: ...
    def close_session(self, session_id, outcome, notes=None) -> None: ...
```

See `core/history/backends/sqlite_backend.py` as the reference implementation.

---

## CI validation

When you submit a PR, GitHub Actions runs:
1. Schema validation on all `*.yaml` manifests (checks required fields)
2. Unit tests
3. Lint (ruff)
4. Check that plugin PRs don't modify `core/`

Your plugin will be rejected at CI if:
- Required YAML fields are missing
- `adapter.py` class is not named `Adapter`
- `backend.py` class is not named `Backend`
- The `id` in YAML doesn't match the folder name

---

## Safety rule syntax reference

SafetyEngine parses rule strings from `domain.yaml safety:` list.

| Pattern | Example | Effect |
|---|---|---|
| `<actuator> OFF if <sensor> > <threshold>` | `pump OFF if soil > 78` | Block pump when too wet |
| `<actuator> OFF if <sensor> < <threshold>` | `ac OFF if temp < 20` | Block AC when cold |
| `<actuator> ON (forced) if <sensor> > <threshold>` | `fan ON (forced) if temp > 32` | Force fan in heat |
| `<actuator> OFF between HH:MM and HH:MM` | `lights OFF between 18:00 and 06:00` | Time-based |

Unsupported patterns: AND/OR conditions, multi-sensor rules, percentage-change rules.
These are planned for a future `SafetyEngine` version.
