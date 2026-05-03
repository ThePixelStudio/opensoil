"""
OpenSoil Web UI — FastAPI server.
Reads from the same SQLite history store as the orchestrator.
Serves a single-page dashboard + REST API + SSE live stream.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.history.backends.sqlite_backend import SQLiteHistoryStore
from core.history.ihistory_store import Event

log = logging.getLogger("opensoil.web")

app = FastAPI(title="OpenSoil UI", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global state (populated by init()) ───────────────────────────────────────

_config: dict = {}
_history: Optional[SQLiteHistoryStore] = None
_profile: dict = {}
_domain_cfg: dict = {}
_chat_processor = None  # ChatProcessor, created in init()


def init(config_path: Path):
    global _config, _history, _profile, _domain_cfg
    with open(config_path) as f:
        _config = yaml.safe_load(f)

    _history = SQLiteHistoryStore(_config["box_id"])

    plugins_root = Path(__file__).parent.parent.parent / "plugins"
    domain_id = _config.get("domain", "smart_garden")

    domain_path = plugins_root / "domains" / domain_id / "domain.yaml"
    if domain_path.exists():
        with open(domain_path) as f:
            _domain_cfg = yaml.safe_load(f)

    profile_id = _config.get("profile")
    if profile_id:
        p = plugins_root / "domains" / domain_id / "profiles" / f"{profile_id}.yaml"
        if p.exists():
            with open(p) as f:
                _profile = yaml.safe_load(f)

    global _chat_processor
    from core.chat.chat_processor import ChatProcessor
    _chat_processor = ChatProcessor(_config, _history, _domain_cfg, _profile)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _box() -> str:
    return _config.get("box_id", "")


def _session_start() -> float:
    if not _history:
        return time.time()
    row = _history.conn.execute(
        "SELECT started_at FROM sessions "
        "WHERE box_id=? AND ended_at IS NULL "
        "ORDER BY started_at ASC LIMIT 1",
        (_box(),),
    ).fetchone()
    return row["started_at"] if row else time.time()


def _day() -> int:
    return int((time.time() - _session_start()) / 86400)


def _phase() -> str:
    if not _profile or "phases" not in _profile:
        return "unknown"
    day = _day()
    for name, data in _profile["phases"].items():
        lo, hi = data.get("days", [0, 9999])
        if lo <= day <= hi:
            return name
    return list(_profile["phases"].keys())[-1]


def _targets() -> dict:
    if not _profile or "phases" not in _profile:
        return {}
    return _profile["phases"].get(_phase(), {}).get("targets", {})


def _chat_path() -> Path:
    p = Path.home() / ".opensoil" / _box() / "chat_history.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_chat() -> list:
    p = _chat_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save_chat(msgs: list):
    _chat_path().write_text(json.dumps(msgs, indent=2))


def _call_text_llm(system: str, user: str) -> str:
    """Call the configured LLM provider for a free-text (non-JSON) response."""
    cfg = _config.get("llm", {})
    provider = cfg.get("provider", "gemini")
    model = cfg.get("model", "")
    api_key = cfg.get("api_key", "")
    try:
        if provider == "gemini":
            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            resp = requests.post(
                url, params={"key": api_key}, timeout=30,
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": 512},
                },
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        if provider == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model, max_tokens=512, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return msg.content[0].text

        if provider in ("openai", "openai_compatible"):
            import openai
            resp = openai.OpenAI(api_key=api_key).chat.completions.create(
                model=model, max_tokens=512,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content

        if provider == "ollama":
            import os
            import requests
            resp = requests.post(
                f"{os.getenv('OLLAMA_HOST', 'http://localhost:11434')}/api/chat",
                json={
                    "model": model, "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]

        return "Provider not supported."
    except Exception as e:
        log.error(f"Chat LLM error: {e}")
        return f"Could not get a response: {e}"


# ── MQTT publish helper ───────────────────────────────────────────────────────

def _mqtt_publish(topic: str, payload: str):
    """One-shot MQTT publish — no persistent connection required."""
    import paho.mqtt.publish as mqtt_publish
    device_cfg = _config.get("device", {}).get("config", {})
    host = device_cfg.get("broker_host", "localhost")
    port = int(device_cfg.get("broker_port", 1883))
    mqtt_publish.single(topic, payload, hostname=host, port=port)


def _topic_prefix() -> str:
    return _config.get("topic_prefix", "opensoil")


def _parse_actuator_command(text: str):
    """Return (actuator_id, value) from a natural-language command, or None."""
    actuators = _domain_cfg.get("actuators", [])
    text_low  = text.lower()
    for a in actuators:
        aid      = a["id"]
        name_low = a.get("name", aid).lower()
        short    = aid.split("_")[-1]
        if not any(kw in text_low for kw in [aid.replace("_", " "), name_low, short]):
            continue
        if any(w in text_low for w in ["on", "start", "activate", "open", "enable"]):
            return (aid, "on")
        if any(w in text_low for w in ["off", "stop", "deactivate", "close", "disable"]):
            return (aid, "off")
    return None


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    decisions = _history.llm_decisions(_box(), limit=1) if _history else []
    last = None
    if decisions:
        d = decisions[0]
        last = {
            "ts": d.ts, "commands": d.commands, "reason": d.reason,
            "model": d.model, "latency_ms": d.latency_ms,
            "was_overridden": d.was_overridden,
        }
    return {
        "box_id": _box(),
        "domain": _config.get("domain"),
        "profile": _config.get("profile"),
        "day": _day(),
        "phase": _phase(),
        "last_decision": last,
    }


@app.get("/api/sensors/latest")
def get_sensors_latest():
    if not _history:
        return {}
    rows = _history.conn.execute(
        """SELECT sensor_id, value, ts, unit FROM sensor_readings
           WHERE box_id=? AND ts=(
             SELECT MAX(ts) FROM sensor_readings s2
             WHERE s2.sensor_id=sensor_readings.sensor_id
               AND s2.box_id=sensor_readings.box_id
           ) ORDER BY sensor_id""",
        (_box(),),
    ).fetchall()

    targets = _targets()
    trends = _history.sensor_trend(_box(), window=604_800)

    result = {}
    for r in rows:
        sid, val = r["sensor_id"], r["value"]
        target = targets.get(sid)
        status = "ok"
        if target:
            if val < target[0]:
                status = "low"
            elif val > target[1]:
                status = "high"
        result[sid] = {
            "value": val,
            "ts": r["ts"],
            "unit": r["unit"] or "",
            "age_sec": int(time.time() - r["ts"]),
            "status": status,
            "target": target,
            "trend": round(trends.get(sid, 0), 2),
        }
    return result


@app.get("/api/history")
def get_history(hours: int = 24):
    if not _history:
        return {}
    since = time.time() - hours * 3600
    rows = _history.conn.execute(
        "SELECT sensor_id, ts, value, unit FROM sensor_readings "
        "WHERE box_id=? AND ts>=? ORDER BY sensor_id, ts",
        (_box(), since),
    ).fetchall()

    series: dict = {}
    for r in rows:
        sid = r["sensor_id"]
        if sid not in series:
            series[sid] = {"points": [], "unit": r["unit"] or ""}
        series[sid]["points"].append({"ts": r["ts"] * 1000, "value": round(r["value"], 2)})
    return series


@app.get("/api/decisions")
def get_decisions(limit: int = 30):
    if not _history:
        return []
    return [
        {
            "ts": d.ts, "phase": d.phase, "commands": d.commands,
            "reason": d.reason, "model": d.model,
            "latency_ms": d.latency_ms, "tokens_used": d.tokens_used,
            "was_overridden": d.was_overridden,
        }
        for d in _history.llm_decisions(_box(), limit=limit)
    ]


@app.get("/api/events")
def get_events(limit: int = 50, hours: int = 48):
    if not _history:
        return []
    events = _history.events(_box(), since=time.time() - hours * 3600)
    return [
        {
            "ts": e.ts, "type": e.type, "severity": e.severity,
            "sensor_id": e.sensor_id, "value": e.value, "note": e.note,
        }
        for e in events[:limit]
    ]


@app.get("/api/profile")
def get_profile():
    day = _day()
    phase = _phase()
    phases = []
    days_until_next = None

    if _profile and "phases" in _profile:
        for name, data in _profile["phases"].items():
            lo, hi = data.get("days", [0, 9999])
            if name == phase:
                days_until_next = max(0, hi - day)
                status = "active"
            elif lo > day:
                status = "upcoming"
            else:
                status = "completed"
            phases.append({
                "name": name, "days": [lo, hi], "status": status,
                "targets": data.get("targets", {}),
                "notes": data.get("notes", ""),
            })

    return {
        "id": _profile.get("id", ""),
        "name": _profile.get("name", _config.get("profile", "")),
        "cultivar": _profile.get("cultivar", ""),
        "harvest_days": _profile.get("harvest_days"),
        "expected_duration_days": _profile.get("expected_duration_days"),
        "notes": _profile.get("notes", ""),
        "current_day": day,
        "current_phase": phase,
        "days_until_next_phase": days_until_next,
        "started_at": _session_start(),
        "phases": phases,
    }


@app.get("/api/actuators")
def get_actuators():
    """Return actuator list with last known state read from history events."""
    actuators = _domain_cfg.get("actuators", [])
    result    = []
    for a in actuators:
        aid = a["id"]
        row = None
        if _history:
            row = _history.conn.execute(
                "SELECT note, ts FROM events WHERE box_id=? AND type='ACTUATOR_FIRED' "
                "AND note LIKE ? ORDER BY ts DESC LIMIT 1",
                (_box(), f"{aid}=%"),
            ).fetchone()
        state = "unknown"
        ts    = None
        if row:
            ts    = row["ts"]
            # note format: "relay_pump=on" or "relay_pump=on (manual)"
            val_part = row["note"].split("=", 1)[-1].split()[0]
            state    = val_part
        result.append({
            "id":    aid,
            "name":  a.get("name", aid),
            "state": state,
            "ts":    ts,
        })
    return result


class ActuatorRequest(BaseModel):
    value: str


@app.post("/api/actuator/{actuator_id}")
def control_actuator(actuator_id: str, req: ActuatorRequest):
    """Publish a manual actuator command directly to the MQTT broker."""
    valid_ids = [a["id"] for a in _domain_cfg.get("actuators", [])]
    if actuator_id not in valid_ids:
        return {"error": f"Unknown actuator: {actuator_id}"}
    if req.value not in ("on", "off"):
        return {"error": "value must be 'on' or 'off'"}

    topic   = f"{_topic_prefix()}/actuator/{actuator_id}/set"
    payload = json.dumps({"value": req.value, "ts": time.time(), "source": "manual"})
    try:
        _mqtt_publish(topic, payload)
    except Exception as e:
        log.error(f"MQTT publish failed for {actuator_id}: {e}")
        return {"error": f"MQTT publish failed: {e}"}

    if _history:
        _history.write_event(Event(
            ts=time.time(), box_id=_box(),
            type="ACTUATOR_FIRED", severity="info",
            note=f"{actuator_id}={req.value} (manual)",
        ))
    return {"ok": True, "actuator_id": actuator_id, "value": req.value}


@app.get("/api/chat_history")
def get_chat_history():
    return _load_chat()


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
def post_chat(req: ChatRequest):
    messages = _load_chat()
    ts     = time.time()
    ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    messages.append({
        "ts": ts, "ts_str": ts_str,
        "role": "user",
        "type": "question" if req.message.strip().endswith("?") else "observation",
        "text": req.message,
    })

    result = _chat_processor.process(req.message)
    messages.append({
        "ts":    time.time(),
        "ts_str": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "role":  "assistant",
        "type":  result["type"],
        "text":  result["response"],
        "model": result.get("model") or "",
    })
    _save_chat(messages)
    return result


# ── SSE live stream ───────────────────────────────────────────────────────────

async def _event_stream() -> AsyncGenerator[str, None]:
    """Push sensor + status snapshot every 30 s."""
    while True:
        try:
            payload = json.dumps({
                "sensors":   get_sensors_latest(),
                "status":    get_status(),
                "actuators": get_actuators(),
            })
            yield f"data: {payload}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        await asyncio.sleep(30)


@app.get("/api/stream")
async def stream():
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Static files (registered last so /api routes win) ────────────────────────

_static = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
