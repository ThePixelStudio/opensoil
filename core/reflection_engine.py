"""
ReflectionEngine — periodic LLM synthesis of sensor history into natural-language insights.

Inspired by the reflection component of the Generative Agents architecture
(Park et al., UIST 2023). Instead of the LLM re-deriving trends from raw
numbers every poll, the engine synthesizes the past 48h into 3-5 durable
insights every 6 hours. These are stored as REFLECTION events and injected
into the SYSTEM prompt, where they are eligible for Anthropic prompt caching.

Cost profile:
  - 1 extra LLM call per 6h  (360 extra calls/day → negligible vs 1440 polls)
  - System prompt stays cache-warm for the full 6h window between reflections
  - Avoids re-serialising 48h of raw history into every USER message
"""

import json
import logging
import time
from datetime import datetime

from .history.ihistory_store import Event

log = logging.getLogger("opensoil.reflection")

DEFAULT_INTERVAL_SEC = 0.5 * 3600    # run every 6 hours
LOOKBACK_SEC         = 48 * 3600   # examine last 48 hours of data

# Event types worth surfacing in a reflection
_SIGNIFICANT = {
    "SENSOR_ANOMALY", "SAFETY_OVERRIDE", "ACTUATOR_NO_EFFECT",
    "DURATION_LIMIT", "LLM_FAILURE_SAFE_OFF", "OBSERVATION",
}


class ReflectionEngine:

    def __init__(self, history, session, llm, domain, profile: dict):
        """
        history : IHistoryStore
        session : SessionState
        llm     : LLMAdapter
        domain  : loaded domain plugin (for sensor / actuator metadata)
        profile : profile dict (phases, targets, cultivar, etc.)
        """
        self._history  = history
        self._session  = session
        self._llm      = llm
        self._domain   = domain
        self._profile  = profile

    # ── Public ────────────────────────────────────────────────────────────────

    def maybe_reflect(self) -> bool:
        """
        Run a reflection if the 6h interval has elapsed.
        Returns True if a new reflection was generated.
        Safe to call every poll — cheap no-op when interval has not elapsed.
        """
        last_ts = self._last_reflection_ts()
        if last_ts and (time.time() - last_ts) < DEFAULT_INTERVAL_SEC:
            return False
        try:
            self._run_reflection()
            return True
        except Exception as e:
            log.error(f"Reflection failed (will retry next interval): {e}")
            return False

    def latest_reflection(self) -> str | None:
        """Return the note text of the most recent REFLECTION event, or None."""
        events = self._history.events(self._session.box_id, type="REFLECTION")
        return events[0].note if events else None

    # ── Core ─────────────────────────────────────────────────────────────────

    def _run_reflection(self):
        context = self._build_prompt()
        text    = self._llm.call_freetext(context)
        self._history.write_event(Event(
            ts=time.time(), box_id=self._session.box_id,
            type="REFLECTION", severity="info", note=text,
        ))
        log.info(
            f"Reflection generated — day {self._session.day_number()}, "
            f"phase '{self._session.current_phase()}': {text[:120]}..."
        )

    def _build_prompt(self) -> dict:
        box_id  = self._session.box_id
        now     = time.time()
        since   = now - LOOKBACK_SEC
        day     = self._session.day_number()
        phase   = self._session.current_phase()
        targets = self._session.current_phase_targets()

        profile_name = self._profile.get("name", "plant")
        cultivar     = self._profile.get("cultivar", "")

        system = (
            f"You are an expert agronomist monitoring {profile_name}"
            + (f" ({cultivar})" if cultivar else "")
            + f" on day {day} of the '{phase}' phase.\n\n"
            f"Phase target ranges:\n{json.dumps(targets, indent=2)}\n\n"
            "Synthesize the provided 48h sensor data, decision log, and events "
            "into 3-5 concise plant-health insights.\n\n"
            "Rules:\n"
            "- Reference specific measured values, not vague descriptions.\n"
            "- Flag trends (e.g. 'soil moisture declining 2%/day for 3 days').\n"
            "- Flag persistent target misses (e.g. 'temp above target 14:00-17:00 daily').\n"
            "- Flag actuator anomalies (no-effect events, duration-limit overrides).\n"
            "- Note what is healthy and stable — not just problems.\n"
            "- Plain text only. No JSON. No markdown headers. 3-5 bullet points."
        )

        # Sensor statistics
        avg_48h  = self._history.sensor_avg(box_id, window=LOOKBACK_SEC)
        mm_48h   = self._history.sensor_minmax(box_id, window=LOOKBACK_SEC)
        trend_7d = self._history.sensor_trend(box_id, window=604_800)

        # Significant events from the last 48h
        all_events = self._history.events(box_id, since=since)
        sig_events = [
            {
                "time": datetime.fromtimestamp(e.ts).strftime("%m-%d %H:%M"),
                "type": e.type,
                "note": e.note or "",
            }
            for e in all_events
            if e.type in _SIGNIFICANT
        ][:30]

        # Last 10 LLM decision reasons — what the system did and why
        decisions = self._history.llm_decisions(box_id, limit=10)
        decision_summary = [
            {
                "time":     datetime.fromtimestamp(d.ts).strftime("%m-%d %H:%M"),
                "commands": d.commands,
                "reason":   d.reason,
            }
            for d in decisions
        ]

        user = (
            f"48h sensor averages:    {json.dumps(avg_48h)}\n"
            f"48h sensor ranges:      {json.dumps(mm_48h)}\n"
            f"7-day trends (per day): {json.dumps(trend_7d)}\n\n"
            f"Significant events (last 48h):\n"
            f"{json.dumps(sig_events, indent=2)}\n\n"
            f"Last 10 LLM decisions:\n"
            f"{json.dumps(decision_summary, indent=2)}\n\n"
            "Generate 3-5 plant-health insights based on this data."
        )

        return {"system": system, "user": user}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _last_reflection_ts(self) -> float | None:
        events = self._history.events(self._session.box_id, type="REFLECTION")
        return events[0].ts if events else None
