# OpenSoil Architecture

## Core principle

> The LLM provides intelligence. OpenSoil provides the IoT operating system.

OpenSoil is a **proactive, time-driven control loop** — not a reactive chat assistant.
Every N seconds it reads sensors, assembles context, calls the LLM, validates safety,
and actuates. No human message required.

---

## Five layers

```
┌─────────────────────────────────────────────────────────────┐
│  Contributor ecosystem                                       │
│  sensors · actuators · devices · domains · history backends │
└───────────────────┬─────────────────────────────────────────┘
                    │ plugin loader (discover · validate · register)
┌───────────────────▼─────────────────────────────────────────┐
│  Core runtime  (never modified by contributors)              │
│  Orchestrator · Context assembler · LLM adapter             │
│  Safety engine · Event bus · Plugin loader                   │
└───────────────────┬─────────────────────────────────────────┘
         write ↓    │ read-back (context)
┌───────────────────▼─────────────────────────────────────────┐
│  History layer  (plugable backends)                          │
│  Time-series store · Event log · LLM decision log           │
│  Rootcause analyzer · Export API                            │
└───────────────────┬─────────────────────────────────────────┘
                    │ IDevice.read() / .write() / .status()
┌───────────────────▼─────────────────────────────────────────┐
│  Hardware abstraction layer  (IDevice interface)             │
│  ESP32/MQTT · Raspberry Pi GPIO · Arduino serial · custom   │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│  Physical hardware                                           │
└─────────────────────────────────────────────────────────────┘
```

---

## Poll loop (60s default)

```
scheduler fires
      │
      ▼
reflection_engine.maybe_reflect()   ← no-op unless 6h have elapsed;
      │                               generates REFLECTION event + updates SYSTEM cache
      ▼
read all sensors → history.write_reading() × N
  (DeviceRouter: per-node reads + aggregate avg/min/max when multi-node)
      │
      ▼
context_assembler.build()
  ├── SYSTEM: domain identity + sensors + actuators + safety rules + JSON schema
  │           + prompt.md + latest REFLECTION (cache-stable 6h)
  └── USER:   live snapshot + 2h avg + 24h range + 7d trend + last 5 decisions
      │
      ▼
llm_adapter.call()  →  raw JSON response
      │
      ▼
safety_engine.validate()
  ├── check threshold rules (soil > 78 → block pump)
  ├── check time rules (18:00 → lights off)
  └── check forced rules (temp > 32 → fan on)
      │
      ▼
device.write(actuator_id, value) × N
      │
      ▼
history.write_decision()  ← full record: prompt + response + overrides
```

---

## Plugin contracts

### sensor.yaml (minimum)
```yaml
id: my_sensor
name: My Sensor
version: 1.0.0
author: yourname
topics:
  - key: my_reading
    unit: celsius
    mqtt: sensors/my_reading
poll_interval_sec: 30
hardware:
  protocol: i2c
```

### actuator.yaml (minimum)
```yaml
id: my_actuator
name: My Actuator
version: 1.0.0
author: yourname
commands:
  - key: my_actuator
    type: on_off
    mqtt: actuator/my_actuator/set
safety: {}
```

### device adapter (adapter.py — 3 methods only)
```python
class Adapter(IDevice):
    def read(self, sensor_id: str) -> float: ...
    def write(self, actuator_id: str, value) -> None: ...
    def status(self) -> dict: ...
```

### domain.yaml (minimum)
```yaml
id: my_domain
name: My Domain
sensors: [{id: my_sensor, required: true}]
actuators: [{id: my_actuator}]
safety: ["my_actuator OFF if my_reading > 100"]
poll: {interval_sec: 60}
```

---

## Context assembly

The LLM receives two messages per poll cycle:

**SYSTEM** (~600 tokens, static per domain):
- Domain identity + profile (day, phase)
- Active sensor list (only connected sensors)
- Actuator list
- Safety rules (always present — never trimmed)
- JSON output schema
- prompt.md domain instructions
- Latest REFLECTION — 3-5 synthesized plant-health insights from last 48h (updated every 6h by ReflectionEngine; stable between updates → Anthropic prompt cache hit rate ~100% within a 6h window)

**USER** (~800 tokens, rebuilt every 60s):
- Live sensor snapshot
- 2h rolling average
- 24h min/max range
- 7-day trend (slope per day)
- Recent anomalies (last 48h)
- Last 5 LLM decisions (prevents re-actuating)
- Session state (phase targets, last actuation times)
- User observations (OBSERVATIONS.md)

Token budget: 3000. Trim order: 7d trend → 24h range → anomalies.
Live snapshot + safety rules + recent decisions are never trimmed.

---

## History schema

Four tables in SQLite (or equivalent in other backends):

| Table | Purpose | Key columns |
|---|---|---|
| `sensor_readings` | Raw sensor values every 30s | ts, box_id, sensor_id, value, phase |
| `events` | Lifecycle + anomaly events | ts, type, severity, note |
| `llm_decisions` | Full LLM call records | ts, full_prompt, commands, was_overridden, reason |
| `sessions` | Grow/occupancy lifecycles | started_at, ended_at, outcome, rootcause |

Event types: `DOMAIN_STARTED`, `PLANT_ADDED`, `PHASE_CHANGE`, `SENSOR_ANOMALY`,
`ACTUATOR_FIRED`, `SAFETY_OVERRIDE`, `OBSERVATION`, `REFLECTION`, `PLANT_DIED`, `HARVEST`

---

## OpenClaw lineage

| Component | Source |
|---|---|
| Plugin discovery (scan → validate → register) | Inherited from OpenClaw |
| Layered prompt composition | Inherited from OpenClaw |
| LLM provider plugins | Inherited from OpenClaw |
| Append-only event log | Inherited from OpenClaw |
| SQLite + credentials store | Inherited from OpenClaw |
| Cron/webhook triggers | Inherited from OpenClaw |
| Poll loop (replaces reactive chat loop) | OpenSoil new |
| IDevice hardware abstraction | OpenSoil new |
| DeviceRouter (multi-node aggregation) | OpenSoil new |
| Time-series sensor store | OpenSoil new |
| Safety engine with physical interlocks | OpenSoil new |
| Domain profiles | OpenSoil new |
| Rootcause analyzer | OpenSoil new |
| ReflectionEngine (periodic synthesis → prompt cache) | OpenSoil new |
