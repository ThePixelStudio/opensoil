# OpenSoil Roadmap

Status key: ✅ Done · 🔨 In progress · 📋 Planned · 💡 Idea

---

## Milestone 0 — Scaffold (COMPLETE)

- ✅ Core interfaces: `IDevice`, `IHistoryStore`
- ✅ Plugin loader (sensor, actuator, device, domain, history backend)
- ✅ Context assembler with 3-horizon history + token trimming
- ✅ Safety engine with threshold + time + forced rules
- ✅ SQLite history backend (4 tables)
- ✅ LLM adapter (Claude + OpenAI + Ollama)
- ✅ Orchestrator poll loop
- ✅ Rootcause analyzer
- ✅ ESP32/MQTT device adapter + ESPHome firmware template
- ✅ Arduino/NodeMCU firmware (ESP8266, DHT11 + resistive soil + dual relay)
- ✅ Multi-node sensor network (`DeviceRouter` — N adapters, per-node MQTT topics, aggregate avg/min/max)
- ✅ Gemini LLM provider
- ✅ ReflectionEngine — periodic 6h LLM synthesis of 48h history into `REFLECTION` events; injected into SYSTEM prompt for Anthropic cache-friendliness
- ✅ Smart garden domain + Swiss chard profile
- ✅ Smart home domain (skeleton)
- ✅ CLI (start, status, history, observe, analyze, plugins, validate)
- ✅ Unit tests (SafetyEngine, PluginLoader, ContextAssembler)
- ✅ Integration test (full poll cycle with mocks)
- ✅ GitHub Actions CI (plugin manifest validation + tests)
- ✅ CONTRIBUTING.md + README.md + architecture.md + decisions.md

---

## Milestone 1 — First real hardware run (NEXT)

Priority: get the system running on actual hardware end-to-end.

### P0 — Must have for first run

- 📋 **Fix import paths** — test suite has relative import issues to resolve
- 📋 **End-to-end smoke test** with real ESP32 + MQTT broker + Claude API
  - Verify sensor readings arrive via MQTT and are cached in `ESP32MqttDevice`
  - Verify LLM is called and JSON is parsed correctly
  - Verify actuator commands are published to correct MQTT topics
  - Verify SQLite history is written correctly
- 📋 **ESPHome firmware test** — flash `esphome_base.yaml` to real ESP32,
  verify sensors publish to correct MQTT topics
- 📋 **`opensoil validate` improvements** — check MQTT broker reachable,
  check API key valid, check device status

### P1 — Should have for milestone 1

- 📋 **Raspberry Pi GPIO device adapter** (`plugins/devices/rpi_gpio/`)
  - Use `gpiozero` for GPIO, `smbus2` for I2C
  - Direct sensor reading — no MQTT hop needed when Pi is the brain
- 📋 **Smart home prompts/base.md** — write the LLM prompt template for
  smart home domain (currently domain.yaml exists but no prompt template)
- 📋 **`motion_pir` sensor plugin** — needed for smart home occupancy
- 📋 **`smart_plug` actuator plugin** — needed for smart home AC/lights
- 📋 **`water_duration_sec` handling in orchestrator** — relay_pump has a
  duration parameter that the orchestrator needs to handle (auto-off timer)
- 📋 **Graceful sensor read failure** — if a sensor returns None, orchestrator
  should continue poll cycle, not crash

### P2 — Nice to have for milestone 1

- 📋 **`opensoil status` live sensor display** — show current sensor values,
  not just last decision
- 📋 **Logging to file** — write logs to `~/.opensoil/logs/<box_id>.log`
  with rotation
- 📋 **`OBSERVATIONS.md` watcher** — auto-reload when file changes, don't
  require restart

---

## Milestone 2 — Plugin ecosystem

Goal: make it easy for contributors to add new sensors, plants, and devices.

- 📋 **`opensoil plugin install <id>`** — download plugin from registry
- 📋 **Plugin registry website** — list all community plugins
- 📋 **More plant profiles:**
  - `thai_basil.yaml` — grows well in HCMC, minimal cooling needed
  - `rau_muong.yaml` — water morning glory, 20-day harvest, native to Vietnam
  - `cherry_tomato.yaml` — fruiting, longer cycle, high light
  - `chili_pepper.yaml` — loves HCMC heat, long harvest period
- 📋 **More sensor plugins:**
  - `co2_mhz19` — CO2 UART sensor (adds major LLM context for indoor grow)
  - `water_flow` — track actual water delivered vs. pump runtime
  - `ph_meter` — for aquarium and hydroponic domains
  - `motion_aqara` — Zigbee motion sensor
- 📋 **More actuator plugins:**
  - `dosing_pump` — for aquarium/hydroponic nutrient dosing
  - `servo_vent` — for ventilation control
  - `ac_ir_blaster` — IR blaster for AC unit control (smart home)
- 📋 **Arduino serial device adapter** (`plugins/devices/arduino_serial/`)
  - JSON over UART protocol
  - No WiFi needed — useful for USB-tethered setups

---

## Milestone 3 — History and observability

Goal: make the history layer genuinely useful for optimization and debugging.

- 📋 **InfluxDB history backend** (`plugins/history_backends/influxdb/`)
  - Grafana dashboard template included
  - Pre-built panels: soil moisture trend, temperature heatmap, actuation timeline
- 📋 **Sensor downsampling** — keep full 30s resolution for 7 days,
  downsample to 5-min averages beyond that (SQLite `sensor_readings_hourly` table)
- 📋 **`opensoil history --plot`** — ASCII sparklines in terminal for quick
  sensor trend visualization
- 📋 **Session comparison** — compare two grow sessions side-by-side:
  `opensoil compare <session_id_1> <session_id_2>`
- 📋 **Automatic rootcause trigger** — when `PLANT_DIED` event is logged,
  automatically queue rootcause analysis and email/notify result
- 📋 **Community dataset export** — anonymized grow sessions uploadable
  to shared dataset for training future models

---

## Milestone 4 — Multi-box and advanced features

- 📋 **Multi-box support** — one OpenSoil process managing multiple boxes
  - Each box has its own config section, device adapter, sensor set
  - Shared LLM adapter (rate limiting across boxes)
  - Shared history store with box_id isolation
- 📋 **Zigbee device adapter** — via Zigbee2MQTT bridge
  - Enables Home Assistant sensor integration without ESP32
  - Covers Aqara, IKEA, Philips Hue ecosystems
- 📋 **HTTP REST device adapter** — for any device with an HTTP API
  - Shelly relays, Tasmota devices, Home Assistant entities
- 📋 **Matter/Thread adapter** — future-proof for smart home
- 📋 **Human override via CLI voice** — "Hey, water the basil now"
  - Inspired by OpenClaw's voice wake feature
  - Injects manual override as event, respects safety rules
- 📋 **Webhook triggers** — external events trigger immediate poll cycle
  - Weather API heat warning → immediate fan check
  - Power outage restored → re-sync actuator states
- 📋 **Aquarium domain** — third reference implementation
  - Sensors: water_temp, ph, ammonia, nitrite, water_level
  - Actuators: heater, dosing_pump, return_pump, skimmer, UV sterilizer
  - Profile: reef_tank, freshwater_planted, betta_tank

---

## Known issues / tech debt

- Import paths in tests may need adjustment (relative vs absolute imports)
- `LLMAdapter._parse_json` has `import re` at module bottom — move to top
- `from_dict()` helper in `orchestrator.py` is a workaround — use proper
  `SensorReading(**d)` pattern instead
- `SQLiteHistoryStore.sensor_trend()` uses numpy — add to required deps
  or implement pure-Python fallback
- `RootcauseAnalyzer.analyze()` directly accesses `history.conn` for updating
  sessions — violates IHistoryStore abstraction, needs `update_session()` method
- ESP32 MQTT adapter has 5-second connection timeout — should be configurable
- `config.yaml` uses `${ENV_VAR}` syntax for API keys — not yet implemented
  in the YAML loader (needs `os.path.expandvars()` or similar)

---

## Ideas parking lot (not prioritized)

- 💡 Web UI dashboard (React) — live sensor charts, last decision display, manual override buttons
- 💡 Mobile app (PWA) — push notifications for anomalies
- 💡 LLM fine-tuning on grow session datasets — specialized plant model
- 💡 Computer vision — camera sensor plugin, LLM sees leaf photos (Event.photo_path already reserved)
- 💡 Predictive "time-to-threshold" — compute "soil hits dry threshold in ~14h at current rate" and inject the ETA into context; no extra LLM call
- 💡 Weather forecast injection — pull Open-Meteo 12h forecast into USER message; skip watering if rain expected
- 💡 Adaptive poll interval — poll faster during anomalies/actuation, slower during stable periods; 60–70 % cost reduction overnight
- 💡 Energy cost optimization — integrate electricity price API, delay high-power actuators to off-peak hours
- 💡 Multi-LLM consensus — call 2 LLMs and only actuate if they agree
- 💡 `opensoil sim` — simulate a grow session with synthetic sensor data for testing plant profiles without hardware
