# OpenSoil — Local Deployment Guide

End-to-end setup: run OpenSoil on your machine with Docker, wire the ESP8266 NodeMCU,
and flash the Arduino firmware so sensors publish live data.

---

## What you need

| Item | Notes |
|---|---|
| Computer (Mac / Linux / Windows) | Runs OpenSoil + MQTT broker via Docker |
| ESP8266 NodeMCU v1.0 (ESP-12E) | The sensor/actuator board |
| DHT11 sensor module | Temperature + humidity |
| Soil moisture sensor (LM393 board) | Has AO (analog) + DO (digital) pins |
| 2× relay module (5 V coil, active-LOW) | Pump relay + fan relay |
| 10 kΩ resistor | Pull-up for DHT11 data line |
| Micro-USB cable | Flash + power the NodeMCU |
| Jumper wires | — |
| Anthropic / Gemini API key | provider of your choice |

---

## Part 1 — Wire the ESP8266 NodeMCU

### Pin reference

```
NodeMCU v1.0
 ┌──────────────────────────────┐
 │  [USB]               [RST]  │
 │  A0  ←── soil AO            │
 │  D1  ──→ relay_pump IN      │  HIGH = pump ON, LOW = pump OFF
 │  D2  ──→ relay_fan  IN      │  HIGH = fan ON,  LOW = fan OFF
 │  D3                         │
 │  D4                         │
 │  D5  ←── DHT11 DATA         │
 │  3V3  ──→ sensors VCC       │
 │  GND  ──→ sensors & relays  │
 │  ...                        │
 └──────────────────────────────┘
```

### DHT11 wiring

DHT11 modules usually come on a 3-pin breakout board (VCC · DATA · GND).

| DHT11 pin | NodeMCU pin | Notes |
|---|---|---|
| VCC | 3V3 | 3.3 V only |
| GND | GND | — |
| DATA | D5 | Add 10 kΩ resistor between DATA and 3V3 |

> **Pull-up resistor** — wire a 10 kΩ between the DATA pin and 3V3.
> Without it the DHT11 will give intermittent read failures.

### Soil moisture sensor (LM393 board) wiring

> **Important — power from 3.3 V, not 5 V.**
> If the board is powered by 5 V, its AO output can reach 5 V,
> which exceeds the NodeMCU A0 maximum of 3.3 V and will damage the pin.

| LM393 board pin | NodeMCU pin | Notes |
|---|---|---|
| VCC | 3V3 | Must be 3.3 V — see warning above |
| GND | GND | — |
| AO | A0 | Analog output — this is the one we read |
| DO | (not used) | Digital threshold output — leave unconnected |

### Relay modules wiring

The firmware drives IN HIGH to close the relay and LOW to open it. This matches
active-HIGH relay boards (bare transistor boards, some optocoupler boards with
logic inverted on-board). If your relay module is active-LOW (IN LOW = relay ON —
common on blue SRD-05VDC boards), swap `HIGH` and `LOW` in the `onMqttMessage`
callback in the sketch.

| Relay board pin | NodeMCU pin | Notes |
|---|---|---|
| VCC | VIN (5 V) | Relay coil needs 5 V — use VIN (USB power), not 3V3 |
| GND | GND | — |
| IN (pump relay) | D1 | GPIO5  — HIGH = pump ON, LOW = pump OFF |
| IN (fan relay)  | D2 | GPIO4  — HIGH = fan ON,  LOW = fan OFF |

The NO (Normally Open) / COM terminals of each relay connect to your pump /
fan power circuit in the usual way.

---

## Part 2 — Flash the ESP8266

### 2.1 Install the ESP8266 board package (once)

1. Open Arduino IDE → **File → Preferences**
2. Paste into *Additional boards manager URLs*:
   ```
   http://arduino.esp8266.com/stable/package_esp8266com_index.json
   ```
3. **Tools → Board Manager** → search `esp8266` → install **esp8266 by ESP8266 Community**

### 2.2 Install libraries (once)

**Tools → Manage Libraries**, search and install each:

| Library name | Author |
|---|---|
| PubSubClient | Nick O'Leary |
| DHT sensor library | Adafruit |
| Adafruit Unified Sensor | Adafruit |
| ArduinoJson | Benoit Blanchon |

### 2.3 Open the sketch

Open:
```
plugins/devices/esp32_mqtt/firmware/esp8266_nodemcu/arduino/opensoil_nodemcu/opensoil_nodemcu.ino
```

### 2.4 Fill in credentials

Edit the defines at the top of the file:

```cpp
#define NODE_ID        "node_left"     // unique name per board (used in MQTT topics)

const char* WIFI_SSID      = "YourWiFiSSID";      // your WiFi name
const char* WIFI_PASSWORD  = "YourWiFiPassword";   // your WiFi password

#define MQTT_HOST      "192.168.1.XXX" // your computer's LAN IP
                                       // Mac  → System Settings → Wi-Fi → Details → IP Address
                                       // Linux → ip addr show | grep "inet "
#define MQTT_USER      ""              // leave empty (broker has no auth by default)
#define MQTT_PASS      ""
```

> Find your computer's LAN IP:
> - **Mac**: System Settings → Wi-Fi → Details → IP Address
> - **Linux**: `ip addr show | grep "inet "`
> - **Windows**: `ipconfig` → IPv4 Address

### 2.5 Select board and port

- **Tools → Board → ESP8266 Boards → NodeMCU 1.0 (ESP-12E Module)**
- **Tools → Port** → select the COM / tty port for your NodeMCU
- Upload speed: 115200 (default)

### 2.6 Flash

Click **Upload** (→ arrow). Wait for *Done uploading*.

### 2.7 Verify in Serial Monitor

Open **Tools → Serial Monitor**, set baud to **115200**.

You should see:
```
[WiFi] Connecting to YourWiFiSSID....
[WiFi] Connected. IP: 192.168.1.42
[MQTT] Connecting as opensoil_node_left...
[MQTT] Connected.
[MQTT] Subscribed to opensoil/nodes/node_left/actuator/relay_pump/set
[MQTT] Subscribed to opensoil/nodes/node_left/actuator/relay_fan/set
[PUB] opensoil/nodes/node_left/sensors/dht11_temp = 27.0
[PUB] opensoil/nodes/node_left/sensors/dht11_humidity = 65.0
[PUB] opensoil/nodes/node_left/sensors/soil_resistive = 29 (raw=612)
```

When OpenSoil sends an actuator command you will see:
```
[CMD] opensoil/nodes/node_left/actuator/relay_pump/set  {"value":"on","ts":...}
[ACT] relay_pump → ON
```

### 2.8 Calibrate soil sensor

The ADC raw value is printed every poll cycle. Use it to calibrate:

1. Leave probe in **dry air** → note the raw value → set `SOIL_DRY` in the sketch
2. Submerge probe in a **glass of water** → note the raw value → set `SOIL_WET`
3. Re-flash

```cpp
#define SOIL_DRY_RAW  980   // ← your measured dry value
#define SOIL_WET_RAW  320   // ← your measured wet value
```

---

## Part 3 — Run OpenSoil locally with Docker

### 3.1 Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- Anthropic API key

### 3.2 Update config.yaml

Two changes required before starting Docker:

```yaml
# 1. Change broker_host from the LAN IP to "mqtt"
#    (Docker Compose routes "mqtt" to the Mosquitto container)
#    The firmware publishes under nodes/<node_id>/ so use the nodes: section.
nodes:
  - id: node_left              # must match NODE_ID in the Arduino sketch
    adapter: esp32_mqtt
    config:
      broker_host: mqtt        # ← change from your LAN IP to "mqtt"
      broker_port: 1883
      topic_prefix: opensoil
    actuators: [relay_pump, relay_fan]

# 2. Paste your API key directly (env var substitution not yet implemented)
llm:
  provider: gemini             # or claude / openai / ollama
  model: gemini-2.0-flash
  api_key: AIza...             # ← paste key here
```

### 3.3 Start the stack

```bash
cd /path/to/opensoil

docker compose up --build
```

This starts two containers:
- **mqtt** — Mosquitto MQTT broker (port 1883)
- **opensoil** — the poll loop (reads MQTT, calls Claude, checks safety, logs history)

First build takes ~2 minutes (downloading Python + dependencies).

### 3.4 Verify everything is connected

Open a second terminal and tail the OpenSoil logs:

```bash
docker compose logs -f opensoil
```

After the ESP8266 publishes its first readings (every 30 s) you should see the
orchestrator pick them up and call the LLM:

```
opensoil  | [INFO] sensor sht31_temp = 27.0
opensoil  | [INFO] sensor dht11_humidity = 65.0
opensoil  | [INFO] calling LLM (claude-sonnet-4-6)...
opensoil  | [INFO] LLM response: {"relay_pump": "off", "relay_fan": "on", ...}
opensoil  | [INFO] decision logged
```

### 3.5 Useful CLI commands

Run these while the stack is up:

```bash
# Last LLM decision + live sensor values
docker-compose exec opensoil opensoil status

# Sensor history (last 24 h)
docker-compose exec opensoil opensoil history --hours 24

# Add a human observation (fed into the next LLM context)
docker-compose exec opensoil opensoil observe "Leaves look a bit pale today"

# List all loaded plugins
docker-compose exec opensoil opensoil plugins

# Validate config + all plugin manifests
docker-compose exec opensoil opensoil validate
```

### 3.6 Stop and restart

```bash
docker compose down          # stop containers (data volumes are kept)
docker compose up            # restart (no rebuild needed unless code changed)
docker compose up --build    # rebuild image after code changes
```

### 3.7 Where data is stored

Sensor history and logs are stored in a named Docker volume (survives restarts):

```bash
# Inspect volume location
docker volume inspect opensoil_opensoil_data

# Copy history DB to host for inspection
docker compose cp opensoil:/root/.opensoil/history ./history-backup
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `DHT11 read failed` in Serial Monitor | Missing 10 kΩ pull-up or loose wire | Add pull-up between D5 and 3V3 |
| Soil always reads 0 % or 100 % | `SOIL_DRY_RAW` / `SOIL_WET_RAW` not calibrated | Follow step 2.8 |
| Relay fires when it should be OFF | Active-LOW module but firmware uses active-HIGH logic | Swap HIGH/LOW in `onMqttMessage` in the sketch |
| Relay never fires even when commanded | Relay VCC connected to 3V3 instead of VIN | Move relay VCC to VIN (5 V from USB) |
| No `[MQTT]` lines for actuator commands | OpenSoil not connected to same broker | Check `broker_host: mqtt` in config.yaml |
| ESP8266 won't connect to MQTT | Wrong broker IP in sketch | Use your computer's LAN IP, not `localhost` |
| `opensoil` container exits immediately | `api_key` still has placeholder | Paste the real key into `config.yaml` |
| LLM not seeing sensor data | `broker_host` still set to LAN IP | Change to `mqtt` in `config.yaml` (step 3.2) |
| Port already in use (1883) | Another MQTT broker running locally | `sudo lsof -i :1883` and stop it |

---

## File map

```
opensoil/
├── config.yaml                                      ← edit this (credentials, broker)
├── docker-compose.yml                               ← starts OpenSoil + Mosquitto
├── Dockerfile                                       ← builds the opensoil image
├── docker/mosquitto.conf                            ← MQTT broker config
└── plugins/
    ├── devices/
    │   └── esp32_mqtt/
    │       └── firmware/
    │           └── esp8266_nodemcu/
    │               └── arduino/
    │                   └── opensoil_nodemcu/
    │                       └── opensoil_nodemcu.ino ← flash this to the board
    ├── sensors/
    │   ├── dht11/sensor.yaml                        ← DHT11 plugin manifest
    │   └── resistive_soil/sensor.yaml               ← soil sensor plugin manifest
    └── actuators/
        ├── relay_pump/actuator.yaml                 ← pump relay plugin manifest
        └── relay_fan/actuator.yaml                  ← fan relay plugin manifest
```
