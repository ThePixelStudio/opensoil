/**
 * OpenSoil — NodeMCU / ESP8266 sensor node firmware
 *
 * Each board is one "node" in a multi-node deployment.
 * Configure NODE_ID, WiFi, and MQTT broker below, then flash.
 *
 * Sensors (wiring):
 *   DHT11 data  → D5 (GPIO14)
 *   Soil ADC    → A0
 *
 * Actuators:
 *   Relay 1     → D1 (GPIO5)   relay_pump
 *   Relay 2     → D2 (GPIO4)   relay_fan
 *
 * MQTT topics published:
 *   opensoil/nodes/<NODE_ID>/sensors/dht11_temp
 *   opensoil/nodes/<NODE_ID>/sensors/dht11_humidity
 *   opensoil/nodes/<NODE_ID>/sensors/soil_resistive
 *
 * MQTT topics subscribed:
 *   opensoil/nodes/<NODE_ID>/actuator/relay_pump/set
 *   opensoil/nodes/<NODE_ID>/actuator/relay_fan/set
 *
 * Libraries required (install via Arduino Library Manager):
 *   - ESP8266WiFi       (bundled with ESP8266 board package)
 *   - PubSubClient      by Nick O'Leary
 *   - DHT sensor library by Adafruit
 *   - ArduinoJson       by Benoit Blanchon (v6+)
 */

#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <ArduinoJson.h>

// ── Configuration ────────────────────────────────────────────────────────

#define NODE_ID        "node_left"      // unique per physical board
#define TOPIC_PREFIX   "opensoil"

const char* WIFI_SSID      = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD  = "YOUR_WIFI_PASSWORD";

#define MQTT_HOST      "192.168.1.XXX"  // your MQTT broker IP
#define MQTT_PORT      1883
#define MQTT_USER      ""               // leave empty if no auth
#define MQTT_PASS      ""

// ── Pin assignments ──────────────────────────────────────────────────────

#define DHT_PIN        14   // D5 on NodeMCU
#define DHT_TYPE       DHT11
#define SOIL_PIN       A0
#define RELAY_PUMP_PIN  5   // D1
#define RELAY_FAN_PIN   4   // D2

// ── Calibration (adjust for your sensor batch) ────────────────────────────
// ADC reads 0–1023 on ESP8266 (0–3.3V via voltage divider on NodeMCU → 0–1V).
// Measure: probe in dry soil → dry_raw; probe in water → wet_raw.
#define SOIL_DRY_RAW  800   // raw ADC value when completely dry
#define SOIL_WET_RAW  300   // raw ADC value when fully saturated

// ── Publish interval ─────────────────────────────────────────────────────

#define PUBLISH_INTERVAL_MS  30000   // 30 seconds

// ── Globals ───────────────────────────────────────────────────────────────

DHT dht(DHT_PIN, DHT_TYPE);
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

char topicSub_pump[80];
char topicSub_fan[80];
unsigned long lastPublish = 0;

// ── Setup ─────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(100);

  pinMode(RELAY_PUMP_PIN, OUTPUT);
  pinMode(RELAY_FAN_PIN,  OUTPUT);
  digitalWrite(RELAY_PUMP_PIN, LOW);  // relays default OFF
  digitalWrite(RELAY_FAN_PIN,  LOW);

  dht.begin();

  // Pre-build subscription topics
  snprintf(topicSub_pump, sizeof(topicSub_pump),
           "%s/nodes/%s/actuator/relay_pump/set", TOPIC_PREFIX, NODE_ID);
  snprintf(topicSub_fan, sizeof(topicSub_fan),
           "%s/nodes/%s/actuator/relay_fan/set", TOPIC_PREFIX, NODE_ID);

  connectWifi();

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  connectMqtt();
}

// ── Loop ──────────────────────────────────────────────────────────────────

void loop() {
  if (!mqtt.connected()) {
    connectMqtt();
  }
  mqtt.loop();

  unsigned long now = millis();
  if (now - lastPublish >= PUBLISH_INTERVAL_MS) {
    lastPublish = now;
    publishSensors();
  }
}

// ── WiFi ──────────────────────────────────────────────────────────────────

void connectWifi() {
  Serial.printf("\n[WiFi] Connecting to %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\n[WiFi] Connected. IP: %s\n", WiFi.localIP().toString().c_str());
}

// ── MQTT ──────────────────────────────────────────────────────────────────

void connectMqtt() {
  String clientId = String("opensoil_") + NODE_ID;
  while (!mqtt.connected()) {
    Serial.printf("[MQTT] Connecting as %s...\n", clientId.c_str());
    bool ok = (strlen(MQTT_USER) > 0)
              ? mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)
              : mqtt.connect(clientId.c_str());
    if (ok) {
      Serial.println("[MQTT] Connected.");
      mqtt.subscribe(topicSub_pump);
      mqtt.subscribe(topicSub_fan);
      Serial.printf("[MQTT] Subscribed to %s\n", topicSub_pump);
      Serial.printf("[MQTT] Subscribed to %s\n", topicSub_fan);
    } else {
      Serial.printf("[MQTT] Failed (state=%d), retrying in 5s\n", mqtt.state());
      delay(5000);
    }
  }
}

// ── Sensor publish ────────────────────────────────────────────────────────

void publishSensors() {
  char topic[100];
  char payload[20];

  // DHT11
  float temp = dht.readTemperature();
  float humi = dht.readHumidity();
  if (!isnan(temp)) {
    snprintf(topic,   sizeof(topic),   "%s/nodes/%s/sensors/dht11_temp", TOPIC_PREFIX, NODE_ID);
    snprintf(payload, sizeof(payload), "%.1f", temp);
    mqtt.publish(topic, payload);
    Serial.printf("[PUB] %s = %s\n", topic, payload);
  } else {
    Serial.println("[WARN] DHT11 read failed — check wiring");
  }

  if (!isnan(humi)) {
    snprintf(topic,   sizeof(topic),   "%s/nodes/%s/sensors/dht11_humidity", TOPIC_PREFIX, NODE_ID);
    snprintf(payload, sizeof(payload), "%.1f", humi);
    mqtt.publish(topic, payload);
    Serial.printf("[PUB] %s = %s\n", topic, payload);
  }

  // Soil resistive ADC — map raw to 0–100%
  int raw  = analogRead(SOIL_PIN);
  int pct  = map(raw, SOIL_DRY_RAW, SOIL_WET_RAW, 0, 100);
  pct      = constrain(pct, 0, 100);
  snprintf(topic,   sizeof(topic),   "%s/nodes/%s/sensors/soil_resistive", TOPIC_PREFIX, NODE_ID);
  snprintf(payload, sizeof(payload), "%d", pct);
  mqtt.publish(topic, payload);
  Serial.printf("[PUB] %s = %s (raw=%d)\n", topic, payload, raw);
}

// ── Actuator command handler ───────────────────────────────────────────────

void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  char buf[128];
  len = min(len, (unsigned int)(sizeof(buf) - 1));
  memcpy(buf, payload, len);
  buf[len] = '\0';

  Serial.printf("[CMD] %s  %s\n", topic, buf);

  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, buf) != DeserializationError::Ok) {
    Serial.println("[CMD] JSON parse error");
    return;
  }

  const char* value = doc["value"] | "";

  if (strcmp(topic, topicSub_pump) == 0) {
    bool on = (strcmp(value, "on") == 0);
    digitalWrite(RELAY_PUMP_PIN, on ? HIGH : LOW);
    Serial.printf("[ACT] relay_pump → %s\n", on ? "ON" : "OFF");
  }
  else if (strcmp(topic, topicSub_fan) == 0) {
    bool on = (strcmp(value, "on") == 0);
    digitalWrite(RELAY_FAN_PIN, on ? HIGH : LOW);
    Serial.printf("[ACT] relay_fan → %s\n", on ? "ON" : "OFF");
  }
}
