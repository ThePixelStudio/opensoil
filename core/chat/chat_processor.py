"""
ChatProcessor — shared message-routing logic for all chat channels.

Used by: web UI (server.py), Telegram engine, and any future chat plugin.
Handles the same three-mode dispatch as the web UI chat:
  actuator command  →  publish to MQTT
  observation       →  write to OBSERVATIONS.md + history event
  question (?)      →  one-off LLM free-text answer
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("opensoil.chat")


class ChatProcessor:

    def __init__(self, config: dict, history, domain_cfg: dict, profile: dict):
        self._config     = config
        self._history    = history
        self._domain_cfg = domain_cfg
        self._profile    = profile

    # ── Public ────────────────────────────────────────────────────────────────

    def process(self, text: str) -> dict:
        """
        Route a message and return a result dict:
          {"type": "command"|"observation"|"answer", "response": str, "model": str|None}
        """
        # 1. Actuator command
        cmd = self._detect_actuator_command(text)
        if cmd:
            aid, val = cmd
            reply = self._send_actuator(aid, val)
            return {"type": "command", "response": reply, "model": None}

        # 2. Observation (no ?)
        if not text.strip().endswith("?"):
            self._save_observation(text)
            return {
                "type": "observation",
                "response": "Saved — will be included in the next LLM poll context.",
                "model": None,
            }

        # 3. Question
        answer, model = self._ask_llm(text)
        return {"type": "answer", "response": answer, "model": model}

    # ── Actuator command ──────────────────────────────────────────────────────

    def _detect_actuator_command(self, text: str):
        """Return (actuator_id, value) or None."""
        actuators = self._domain_cfg.get("actuators", [])
        text_low  = text.lower()
        for a in actuators:
            aid      = a["id"]
            name_low = a.get("name", aid).lower()
            short    = aid.split("_")[-1]
            keywords = [aid.replace("_", " "), name_low, short]
            if not any(kw in text_low for kw in keywords if kw):
                continue
            if any(w in text_low for w in ["on", "start", "activate", "open", "enable"]):
                return (aid, "on")
            if any(w in text_low for w in ["off", "stop", "deactivate", "close", "disable"]):
                return (aid, "off")
        return None

    def _send_actuator(self, actuator_id: str, value: str) -> str:
        from core.history.ihistory_store import Event
        topic_prefix = self._config.get("topic_prefix", "opensoil")
        topic   = f"{topic_prefix}/actuator/{actuator_id}/set"
        payload = json.dumps({"value": value, "ts": time.time(), "source": "chat"})
        try:
            self._mqtt_publish(topic, payload)
        except Exception as e:
            log.error(f"MQTT publish failed for {actuator_id}: {e}")
            return f"MQTT error: {e}"
        if self._history:
            self._history.write_event(Event(
                ts=time.time(), box_id=self._config["box_id"],
                type="ACTUATOR_FIRED", severity="info",
                note=f"{actuator_id}={value} (chat)",
            ))
        return f"Sent: {actuator_id} → {value}"

    def _mqtt_publish(self, topic: str, payload: str):
        import paho.mqtt.publish as mqtt_pub
        device_cfg = self._config.get("device", {}).get("config", {})
        mqtt_pub.single(
            topic, payload,
            hostname=device_cfg.get("broker_host", "localhost"),
            port=int(device_cfg.get("broker_port", 1883)),
        )

    # ── Observation ───────────────────────────────────────────────────────────

    def _save_observation(self, text: str):
        from core.history.ihistory_store import Event
        ts_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
        obs_dir = Path.home() / ".opensoil" / self._config["box_id"]
        obs_dir.mkdir(parents=True, exist_ok=True)
        with open(obs_dir / "OBSERVATIONS.md", "a") as f:
            f.write(f"[{ts_str}] {text}\n")
        if self._history:
            self._history.write_event(Event(
                ts=time.time(), box_id=self._config["box_id"],
                type="OBSERVATION", severity="info", note=text,
            ))

    # ── LLM question ─────────────────────────────────────────────────────────

    def _ask_llm(self, question: str) -> tuple[str, str]:
        """Returns (answer, model_name)."""
        sensors      = self._sensor_snapshot()
        phase        = self._current_phase()
        targets      = self._current_targets()
        history_ctx  = self._history_context()
        profile_name = self._profile.get("name", "a plant")
        cultivar     = self._profile.get("cultivar", "")

        system = (
            f"You are an expert monitoring {profile_name}"
            f"{(' (' + cultivar + ')') if cultivar else ''} "
            f"on day {self._day_number()} of the '{phase}' phase.\n"
            "You have access to current sensor readings, 24h statistics, "
            "hourly sensor breakdown, recent actuator decisions, and recent events.\n"
            "Answer concisely and specifically. Always reference actual values from the data."
        )
        sensor_summary = {k: f"{v['value']} {v['unit']}" for k, v in sensors.items()}
        user = (
            f"Current sensors: {json.dumps(sensor_summary)}\n"
            f"Phase targets: {json.dumps(targets)}\n"
            f"24h stats (avg/min/max): {json.dumps(history_ctx['stats_24h'])}\n"
            f"Hourly breakdown (today): {json.dumps(history_ctx['hourly'])}\n"
            f"Recent decisions: {json.dumps(history_ctx['recent_decisions'])}\n"
            f"Recent events: {json.dumps(history_ctx['recent_events'])}\n"
            f"Question: {question}"
        )

        cfg    = self._config.get("llm", {})
        model  = cfg.get("model", "")
        answer = self._call_llm(system, user, cfg)
        return answer, model

    def _history_context(self) -> dict:
        """Build a rich context dict from history for LLM questions."""
        empty = {"stats_24h": {}, "hourly": {}, "recent_decisions": [], "recent_events": []}
        if not self._history:
            return empty

        box_id   = self._config["box_id"]
        now      = time.time()
        since_24 = now - 86400

        # 24h aggregate stats
        avg_24h = self._history.sensor_avg(box_id, window=86400)
        mm_24h  = self._history.sensor_minmax(box_id, window=86400)
        stats = {
            sid: {
                "avg": round(avg_24h[sid], 1),
                "min": round(mm_24h.get(sid, {}).get("min", 0), 1),
                "max": round(mm_24h.get(sid, {}).get("max", 0), 1),
            }
            for sid in avg_24h
        }

        # Hourly breakdown — lets LLM answer "hottest time", "when was soil dry", etc.
        rows = self._history.conn.execute(
            """SELECT sensor_id,
                      strftime('%H:00', datetime(ts, 'unixepoch', 'localtime')) AS hour,
                      ROUND(AVG(value), 1) AS avg_val,
                      ROUND(MAX(value), 1) AS max_val,
                      ROUND(MIN(value), 1) AS min_val
               FROM sensor_readings
               WHERE box_id=? AND ts>=?
               GROUP BY sensor_id, hour
               ORDER BY sensor_id, hour""",
            (box_id, since_24),
        ).fetchall()
        hourly: dict = {}
        for r in rows:
            sid = r["sensor_id"]
            hourly.setdefault(sid, []).append({
                "hour": r["hour"],
                "avg":  r["avg_val"],
                "max":  r["max_val"],
                "min":  r["min_val"],
            })

        # Last 5 LLM decisions
        decisions = self._history.llm_decisions(box_id, limit=5)
        recent_decisions = [
            {
                "time":     datetime.fromtimestamp(d.ts).strftime("%H:%M"),
                "commands": d.commands,
                "reason":   d.reason,
            }
            for d in decisions
        ]

        # Recent events (last 24h, max 20)
        events = self._history.events(box_id, since=since_24)
        recent_events = [
            {
                "time": datetime.fromtimestamp(e.ts).strftime("%H:%M"),
                "type": e.type,
                "note": e.note or "",
            }
            for e in events[:20]
        ]

        return {
            "stats_24h":        stats,
            "hourly":           hourly,
            "recent_decisions": recent_decisions,
            "recent_events":    recent_events,
        }

    def _day_number(self) -> int:
        return int((time.time() - self._session_start()) / 86400)

    def _call_llm(self, system: str, user: str, cfg: dict) -> str:
        provider = cfg.get("provider", "gemini")
        model    = cfg.get("model", "")
        api_key  = cfg.get("api_key", "")
        try:
            if provider == "gemini":
                import requests
                url  = (
                    f"https://generativelanguage.googleapis.com/v1beta/models"
                    f"/{model}:generateContent"
                )
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
                msg = anthropic.Anthropic(api_key=api_key).messages.create(
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
                        {"role": "user",   "content": user},
                    ],
                )
                return resp.choices[0].message.content

            if provider == "ollama":
                import os, requests
                resp = requests.post(
                    f"{os.getenv('OLLAMA_HOST', 'http://localhost:11434')}/api/chat",
                    json={
                        "model": model, "stream": False,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user",   "content": user},
                        ],
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"]

            return "LLM provider not supported."
        except Exception as e:
            log.error(f"LLM call failed: {e}")
            return f"Could not get a response: {e}"

    # ── Context helpers ───────────────────────────────────────────────────────

    def _sensor_snapshot(self) -> dict:
        if not self._history:
            return {}
        rows = self._history.conn.execute(
            """SELECT sensor_id, value, ts, unit FROM sensor_readings
               WHERE box_id=? AND ts=(
                 SELECT MAX(ts) FROM sensor_readings s2
                 WHERE s2.sensor_id=sensor_readings.sensor_id
                   AND s2.box_id=sensor_readings.box_id
               )""",
            (self._config["box_id"],),
        ).fetchall()
        return {r["sensor_id"]: {"value": r["value"], "unit": r["unit"] or ""} for r in rows}

    def _current_phase(self) -> str:
        if not self._profile or "phases" not in self._profile:
            return "unknown"
        start = self._session_start()
        day   = int((time.time() - start) / 86400)
        for name, data in self._profile["phases"].items():
            lo, hi = data.get("days", [0, 9999])
            if lo <= day <= hi:
                return name
        return list(self._profile["phases"].keys())[-1]

    def _current_targets(self) -> dict:
        if not self._profile or "phases" not in self._profile:
            return {}
        return self._profile["phases"].get(self._current_phase(), {}).get("targets", {})

    def _session_start(self) -> float:
        if not self._history:
            return time.time()
        row = self._history.conn.execute(
            "SELECT started_at FROM sessions "
            "WHERE box_id=? AND ended_at IS NULL "
            "ORDER BY started_at ASC LIMIT 1",
            (self._config["box_id"],),
        ).fetchone()
        return row["started_at"] if row else time.time()
