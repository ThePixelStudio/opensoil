# Architectural Decisions

Every significant design choice is recorded here with its rationale.
When you're about to change something fundamental, check here first.

---

## ADR-001: Proactive poll loop, not reactive chat loop

**Decision:** The orchestrator fires on a scheduler every N seconds, not in
response to user messages.

**Context:** We considered building on top of OpenClaw's gateway (which routes
chat messages to agents). The problem: sensors don't wait for humans to ask
questions. A plant's soil moisture needs to be checked every 30 seconds
regardless of whether anyone is talking to the system.

**Consequences:**
- The orchestrator is simpler — one loop, one trigger
- No WebSocket gateway needed for the control plane
- Human interaction (overrides, observations) is a separate side channel (CLI)
- Multi-turn conversation history is irrelevant — replaced by sensor history

---

## ADR-002: IDevice interface with exactly 3 methods

**Decision:** `read(sensor_id) → float`, `write(actuator_id, value) → None`,
`status() → dict`. The orchestrator calls nothing else.

**Context:** We needed hardware to be swappable without touching any core code.
Having 3 methods means any board (ESP32, Pi, Arduino, Zigbee coordinator, HTTP
REST device) can be a device adapter by implementing a trivial class.

**Consequences:**
- Contributors adding Zigbee support never touch the orchestrator
- Testing the orchestrator is trivial — mock 3 methods
- Device capabilities are declared in `device.yaml`, not enforced by the interface
- Swapping hardware = one line change in `config.yaml`

---

## ADR-003: Safety rules are declarative in domain.yaml, enforced in core

**Decision:** Safety rules are written as strings in `domain.yaml` by domain
authors, but the `SafetyEngine` (core) parses and enforces them. Domain authors
cannot write custom Python safety logic.

**Context:** We considered letting domain authors write Python safety callbacks.
The problem: a buggy or malicious plugin could bypass safety entirely. Physical
safety (water pump, AC, locks) must be enforced by trusted core code.

**Rule syntax deliberately simple:**
```
"relay_pump OFF if soil_cap > 78"
"relay_fan ON (forced) if sht31_temp > 32"
"pwm_light OFF between 18:00 and 06:00"
```

**Consequences:**
- Contributors can declare safety constraints without writing Python
- Core always has final say — no plugin can actuate past safety
- Every safety override is logged as `SAFETY_OVERRIDE` event
- Complex conditional logic requires a new rule syntax (planned: AND/OR)

---

## ADR-004: SQLite as default history backend

**Decision:** Default history storage is a single SQLite file at
`~/.opensoil/history/<box_id>.db`. No configuration required.

**Context:** The target hardware is a Raspberry Pi Zero 2W. We evaluated:
- InfluxDB: great for time-series, but requires separate process + 512MB RAM
- Prometheus: pull-model doesn't fit write-heavy sensor pattern
- CSV: simple but no indexing, slow range queries
- SQLite: zero setup, 0MB overhead, good enough for 1-10 sensors at 30s intervals

**Capacity math:** 5 sensors × 2 readings/min × 60 min × 24h × 365 days
= ~5.25M rows/year ≈ 200MB. Comfortable for a Pi SD card.

**Consequences:**
- Zero-config default — works out of the box
- InfluxDB backend available for users who want Grafana dashboards
- `IHistoryStore` interface makes backend swappable without touching orchestrator

---

## ADR-005: Context assembly split between SYSTEM and USER messages

**Decision:** Safety rules and domain identity go in the SYSTEM message (static).
Live sensors, history, and recent decisions go in the USER message (rebuilt
every 60s).

**Context:** Token budget is finite. If everything is in one message and we need
to trim, we might accidentally trim safety rules. By splitting:
- SYSTEM = never trimmed, always fully sent
- USER = trimmed gracefully when over budget (7d → 24h → anomalies)

**Trim order (USER message):**
1. Drop 7-day trend (useful context, but slowest-changing)
2. Drop 24h min/max range
3. Drop anomaly list
4. Live snapshot + last 5 decisions + session state: **never dropped**

**Consequences:**
- Safety rules are guaranteed to be in every LLM call
- Context trimming is predictable and testable
- Token budget: 3000 (system ~600 + user ~800 = ~1400 typical; 3000 is ceiling)

---

## ADR-006: Last 5 LLM decisions injected into every context

**Decision:** Every USER message includes the last 5 LLM decisions with their
commands and reasoning.

**Context:** LLMs are stateless. Without decision history, the LLM will re-water
every 60s because it "forgot" it watered 10 minutes ago. This was observed in
early prototyping.

**Alternative considered:** Only inject if actuation happened recently. Rejected
because the LLM needs to know what it *didn't* do as much as what it did.

**Consequences:**
- Prevents redundant actuation (most important)
- Gives LLM continuity without full conversation history
- Adds ~200 tokens per poll cycle
- Creates a natural audit trail visible in the prompt

---

## ADR-007: Domain profiles as YAML, not code

**Decision:** Plant profiles (swiss_chard.yaml, thai_basil.yaml etc.) are pure
YAML data files declaring growth phases and sensor targets. No Python required.

**Context:** We want non-developers to contribute plant profiles. A botanist
should be able to add a new plant by writing YAML, not Python.

**What a profile declares:**
```yaml
phases:
  vegetative:
    days: [25, 55]
    targets:
      sht31_temp: [20, 28]
      soil_cap:   [55, 70]
```

The LLM uses these targets as context — it knows what optimal means for this
plant at this stage without any hardcoded threshold logic.

**Consequences:**
- Botanists can contribute, not just developers
- The LLM's knowledge supplements the profile — no need to enumerate every edge case
- Phase targets are injected into SESSION STATE in the USER message

---

## ADR-008: One config.yaml per box

**Decision:** Each physical box has exactly one `config.yaml`. The user only
edits this file — never plugin manifests, never core code.

**Context:** OpenClaw has a similar pattern with its JSON config. We adopted
this because it makes deployment simple: `git clone`, edit `config.yaml`, run.

**What config.yaml controls:**
- Which domain and profile
- Which device adapter (and its connection config)
- Which sensor + actuator plugins are active
- Which LLM provider
- Which history backend

**Consequences:**
- Plugin authors write for portability, not for a specific box
- Config validation (`opensoil validate`) catches errors before hardware is connected
- Multiple boxes = multiple config files, one OpenSoil process per box

---

## ADR-009: OpenClaw architectural inheritance

**Inherited directly:**
- Plugin discovery (scan folder → validate YAML schema → register)
- Layered prompt composition (domain.yaml + prompt.md = AGENTS.md + SOUL.md)
- LLM provider abstraction (swap providers in config)
- Append-only event log (OpenClaw's session log → OpenSoil's history tables)
- SQLite as zero-config storage
- Credentials directory with 0600 file permissions
- Cron/webhook triggering

**Adapted:**
- Session concept (conversation → grow cycle / occupancy period)
- Context assembly (chat history → sensor time-series windows)
- Memory compaction (conversation summarization → sensor downsampling)

**Replaced:**
- Gateway WebSocket server → MQTT bus + scheduler
- Reactive message loop → proactive poll loop
- Channel adapters (WhatsApp etc.) → device adapters (ESP32, Pi, Arduino)
- Canvas/A2UI → Grafana dashboard (planned)

**New in OpenSoil (no OpenClaw equivalent):**
- IDevice hardware abstraction layer
- Time-series sensor store
- Safety engine with physical interlocks
- Domain profiles with growth phases
- Rootcause analyzer

---

## ADR-010: Selective sensor injection (only active sensors in prompt)

**Decision:** Only sensors that are actually connected and publishing data are
included in the LLM system prompt. If a CO2 sensor plugin exists but isn't
wired up, it doesn't appear in the prompt.

**Context:** Inherited from OpenClaw's "selective skill injection" principle —
inject only what's relevant to the current turn. If a sensor appears as `null`
in the snapshot, the LLM wastes tokens trying to reason about it.

**Consequences:**
- Prompts stay lean even if many sensor plugins are installed
- LLM context is always a true reflection of the physical box
- Disconnected sensors trigger `SENSOR_ANOMALY` events separately

---

## ADR-011: Multi-node sensor network via DeviceRouter

**Decision:** When `nodes:` is present in `config.yaml`, a `DeviceRouter` wraps
N device adapters. Each node gets namespaced MQTT topics
(`opensoil/nodes/<node_id>/sensors/#`). The router exposes aggregate sensor IDs
(avg, `_min`, `_max`) alongside node-specific IDs to the orchestrator.

**Context:** A single ESP8266/ESP32 can't reach every corner of a large grow box
or room. We needed a way to deploy N sensor boards while keeping the orchestrator's
`IDevice` contract unchanged.

**Consequences:**
- Orchestrator never knows whether it's talking to one board or five
- Partial failure (one node offline) degrades gracefully — aggregate drops to
  remaining nodes, per-node IDs disappear from snapshot, warning logged once
- MQTT client IDs must be unique per node (`opensoil_ctrl_<node_id>`) to avoid
  the broker kicking duplicate connections (rc=7 disconnect loop)
- Aggregate IDs (`dht11_temp`) live alongside node-specific IDs
  (`node_left_dht11_temp`) so both the LLM and the history store capture both

---

## ADR-012: ReflectionEngine for cache-efficient periodic synthesis

**Decision:** Every 6 hours, a separate LLM call synthesizes the last 48h of
sensor statistics, significant events, and LLM decision reasons into 3-5
plain-text plant-health insights stored as `REFLECTION` events. The latest
reflection is injected into the SYSTEM prompt (not USER) between reflections.

**Context:** Inspired by the reflection component of Generative Agents (Park
et al., UIST 2023). The original problem: including 48h of raw history in every
USER message cost ~$6/day with Claude and produced noisy context. Alternative:
the LLM re-derives trends from raw numbers every 60s poll — expensive and
redundant.

**Why SYSTEM, not USER:**
Anthropic prompt caching works on the SYSTEM message prefix. If the SYSTEM message
is identical for 6 hours (between reflections), every poll in that window hits the
cache. Putting the reflection in USER would bust the cache every poll.

**Cost profile:**
- 1 extra LLM call per 6h (4/day) — negligible vs. 720 regular polls/day
- System prompt cache stays warm for the full 6h window → ~99% cache hit rate
- Avoids re-serialising 48h of raw history into every USER message

**Consequences:**
- `call_freetext()` added to `LLMAdapter` — returns plain text, disables
  `responseMimeType: application/json` (Gemini) and `"format": "json"` (Ollama)
- `REFLECTION` is a new event type in the history schema
- Context assembler gains optional `reflection_engine` param (backwards-compatible)
