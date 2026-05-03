"""
ContextAssembler — builds the full LLM prompt every poll cycle.

Maps to OpenClaw's context assembly:
  OpenClaw: AGENTS.md + SOUL.md + skills + memory search + session history
  OpenSoil: domain.yaml + prompt.md + live sensors + history windows + decisions

System message: static identity, safety rules, output schema  (~600 tokens)
User message:   live sensors + 3-horizon history + last 5 decisions (~800 tokens)
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("opensoil.context_assembler")

TOKEN_BUDGET = 2800  # conservative; leaves ~1200 tokens for LLM response


def estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 characters."""
    return len(text) // 4


class ContextAssembler:

    def __init__(self, domain, session_state, history_store, device, reflection_engine=None):
        self.domain      = domain
        self.session     = session_state
        self.history     = history_store
        self.device      = device
        self.reflection  = reflection_engine

    def build(self) -> dict:
        """Returns {"system": str, "user": str} ready for LLM adapter."""
        return {
            "system": self._build_system(),
            "user":   self._build_user(),
        }

    # ── SYSTEM — static per domain ────────────────────────────────────────

    def _build_system(self) -> str:
        parts = []

        # 1. Domain identity
        parts.append(
            f"You are an autonomous IoT controller.\n"
            f"Domain  : {self.domain.name}\n"
            f"Profile : {self.session.profile_id}\n"
            f"Box ID  : {self.session.box_id}\n"
            f"Day     : {self.session.day_number()} "
            f"(phase: {self.session.current_phase()})"
        )

        # 2. Active sensors only — OpenClaw principle: inject only what's registered
        sensor_lines = "\n".join(
            f"  - {s['id']}: {s.get('name', s['id'])}"
            for s in self.domain.sensors
            if s.get("required", False) or self._sensor_connected(s["id"])
        )
        parts.append(f"SENSORS:\n{sensor_lines}")

        # 3. Available actuators
        actuator_lines = "\n".join(
            f"  - {a['id']}: {a.get('name', a['id'])}"
            for a in self.domain.actuators
        )
        parts.append(f"ACTUATORS:\n{actuator_lines}")

        # 4. Safety rules — always in system, never trimmed
        safety_lines = "\n".join(
            f"  RULE: {r}" for r in self.domain.safety
        )
        parts.append(
            f"SAFETY RULES — NEVER VIOLATE THESE:\n{safety_lines}"
        )

        # 5. Output schema — type-aware per actuator
        def _actuator_schema(aid: str) -> str:
            if aid.startswith("pwm_"):
                return f'  "{aid}": <integer 0-100>   // PWM duty cycle percent'
            return f'  "{aid}": "on" | "off"'

        schema_fields = "\n".join(
            _actuator_schema(a["id"]) for a in self.domain.actuators
        )
        parts.append(
            f'Respond ONLY in valid JSON — no text outside the object.\n'
            f'ALL fields are REQUIRED every response — include every actuator even if unchanged:\n'
            f'{{\n{schema_fields},\n  "reason": "<one specific sentence>"\n}}'
        )

        # 6. prompt.md — domain author's custom instructions
        if self.domain.prompt_template_path.exists():
            parts.append(self.domain.prompt_template_path.read_text())

        # 7. Latest reflection — synthesized 48h insights (cache-friendly: stable for 6h)
        if self.reflection:
            latest = self.reflection.latest_reflection()
            if latest:
                parts.append(f"PLANT HEALTH SUMMARY (last 48h synthesis):\n{latest}")

        return "\n\n".join(parts)

    # ── USER — rebuilt every poll cycle ──────────────────────────────────

    def _build_user(self) -> str:
        now = time.time()
        parts = []

        # 2a. Live sensor snapshot
        snapshot = self._read_sensors()
        parts.append(
            f"CURRENT SENSORS (live at {datetime.now().strftime('%H:%M:%S')}):\n"
            f"{json.dumps(snapshot, indent=2)}"
        )

        # 2b. Three history horizons
        h2h  = self.history.sensor_avg(self.session.box_id, window=7_200)
        h24h = self.history.sensor_minmax(self.session.box_id, window=86_400)
        h7d  = self.history.sensor_trend(self.session.box_id, window=604_800)

        history_block = (
            f"HISTORY:\n"
            f"  2h avg    : {json.dumps(h2h)}\n"
            f"  24h range : {json.dumps(h24h)}\n"
            f"  7d trend  : {json.dumps(h7d)}"
        )
        parts.append(history_block)

        # 2c. Recent anomalies (last 48h)
        anomalies = self.history.events(
            self.session.box_id,
            type="SENSOR_ANOMALY",
            since=now - 172_800
        )
        if anomalies:
            lines = [
                f"  [{datetime.fromtimestamp(e.ts).strftime('%m-%d %H:%M')}] "
                f"{e.sensor_id}={e.value} — {e.note}"
                for e in anomalies[:5]
            ]
            parts.append("RECENT ANOMALIES:\n" + "\n".join(lines))

        # 2d. Last 5 LLM decisions — prevents re-watering 10min after watering
        decisions = self.history.llm_decisions(self.session.box_id, limit=5)
        if decisions:
            lines = [
                f"  [{datetime.fromtimestamp(d.ts).strftime('%H:%M')}] "
                f"{json.dumps(d.commands)} — {d.reason}"
                for d in decisions
            ]
            parts.append("LAST 5 DECISIONS:\n" + "\n".join(lines))

        # 2e. Session state — targets are advisory, not hard rules
        targets = self.session.current_phase_targets()
        parts.append(
            f"SESSION STATE:\n"
            f"  Advisory ranges (use judgment): {json.dumps(targets)}\n"
            f"  Time now      : {datetime.now().strftime('%H:%M')} "
            f"{datetime.now().strftime('%A')}\n"
            f"  Last actuations: {json.dumps(self.session.last_actuation_times())}"
        )

        # 2f. User observations from OBSERVATIONS.md
        obs = self._load_observations()
        if obs:
            parts.append(f"USER OBSERVATIONS:\n{obs}")

        # 2g. Trim to token budget — drop oldest history first, keep live always
        return self._trim_to_budget("\n\n".join(parts))

    # ── Helpers ──────────────────────────────────────────────────────────

    def _read_sensors(self) -> dict:
        snapshot = {}
        for sensor in self.domain.sensors:
            sid = sensor["id"]
            try:
                snapshot[sid] = self.device.read(sid)
            except Exception as e:
                snapshot[sid] = None
                log.warning(f"Could not read sensor {sid}: {e}")
        return snapshot

    def _sensor_connected(self, sensor_id: str) -> bool:
        try:
            self.device.read(sensor_id)
            return True
        except Exception:
            return False

    def _load_observations(self) -> Optional[str]:
        obs_path = Path.home() / ".opensoil" / self.session.box_id / "OBSERVATIONS.md"
        if not obs_path.exists():
            return None
        lines = obs_path.read_text().strip().splitlines()
        # Return last 3 observations only
        return "\n".join(lines[-3:]) if lines else None

    def _trim_to_budget(self, text: str) -> str:
        """Gracefully trim to token budget: drop 7d first, then 24h, keep live always."""
        if estimate_tokens(text) <= TOKEN_BUDGET:
            return text

        # Drop 7d trend
        text = re.sub(r"  7d trend\s*:.*?\n", "", text)
        if estimate_tokens(text) <= TOKEN_BUDGET:
            log.debug("Context trimmed: dropped 7d trend")
            return text

        # Drop 24h range
        text = re.sub(r"  24h range\s*:.*?\n", "", text)
        if estimate_tokens(text) <= TOKEN_BUDGET:
            log.debug("Context trimmed: dropped 24h range")
            return text

        # Drop anomalies section
        text = re.sub(r"RECENT ANOMALIES:.*?\n\n", "", text, flags=re.DOTALL)
        log.warning("Context heavily trimmed — consider reducing poll frequency")
        return text
