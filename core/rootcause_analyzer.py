"""
RootcauseAnalyzer — post-mortem LLM analysis when a session ends badly.
Triggered automatically when PLANT_DIED event is logged,
or manually via: opensoil analyze <session_id>

Pulls all sensor data, events, and LLM decisions for the full session
and sends them to the LLM for structured diagnosis.
"""

import json
import logging
import time
from datetime import datetime
from itertools import groupby

from .history.ihistory_store import IHistoryStore, Event
from .llm.llm_adapter import LLMAdapter

log = logging.getLogger("opensoil.rootcause")


class RootcauseAnalyzer:

    def __init__(self, history: IHistoryStore, llm: LLMAdapter):
        self.history = history
        self.llm     = llm

    def analyze(self, box_id: str, session_id: str) -> dict:
        """
        Run post-mortem analysis on a completed session.
        Returns structured diagnosis dict.
        """
        log.info(f"Starting rootcause analysis for session {session_id}")

        # 1. Pull sensor statistics for full session duration
        stats = self.history.sensor_minmax(box_id, window=999_999_999)
        avgs  = self.history.sensor_avg(box_id, window=999_999_999)
        trend = self.history.sensor_trend(box_id, window=999_999_999)

        # 2. All events — anomalies, overrides, observations, actuations
        events = self.history.events(box_id)
        anomalies   = [e for e in events if e.type == "SENSOR_ANOMALY"]
        overrides   = [e for e in events if e.type == "SAFETY_OVERRIDE"]
        observations= [e for e in events if e.type == "OBSERVATION"]
        errors      = [e for e in events if e.severity == "critical"]

        # 3. Last 20 LLM decisions before session end
        decisions = self.history.llm_decisions(box_id, limit=20)

        # 4. Build post-mortem prompt
        prompt_system = """You are a plant pathologist and IoT systems expert.
You are analyzing a completed grow session to identify root causes of failure or success.
Be specific, quantitative, and actionable. 
Respond ONLY in valid JSON."""

        prompt_user = f"""
GROW SESSION ANALYSIS REQUEST

Sensor statistics (full session):
{json.dumps({"averages": avgs, "ranges": stats, "trends": trend}, indent=2)}

Anomaly events ({len(anomalies)} total):
{self._format_events(anomalies[:10])}

Safety overrides ({len(overrides)} total — LLM blocked by safety engine):
{self._format_events(overrides[:10])}

Critical errors ({len(errors)} total):
{self._format_events(errors[:5])}

User observations:
{self._format_events(observations)}

Last 20 LLM decisions:
{self._format_decisions(decisions)}

Provide diagnosis as JSON:
{{
  "primary_cause": "most likely root cause in one sentence",
  "contributing_factors": [
    {{"factor": "...", "evidence": "...", "severity": "high|medium|low"}}
  ],
  "sensor_issues": ["list any sensor data that looks suspicious"],
  "llm_decision_quality": "assessment of whether LLM decisions were appropriate",
  "safety_engine_notes": "were safety overrides appropriate or too aggressive?",
  "recommendations": [
    "specific actionable change for next grow"
  ],
  "confidence": "high|medium|low",
  "overall_assessment": "2-3 sentence summary"
}}"""

        # 5. Call LLM for diagnosis
        try:
            response = self.llm.call({
                "system": prompt_system,
                "user":   prompt_user,
            })
            diagnosis = response.commands  # LLMAdapter parses JSON
            diagnosis["reason"] = response.reason
        except Exception as e:
            log.error(f"Rootcause LLM call failed: {e}")
            diagnosis = {"error": str(e), "primary_cause": "Analysis failed — check logs"}

        # 6. Store rootcause in session record
        self.history.conn.execute(
            "UPDATE sessions SET rootcause=? WHERE id=?",
            (json.dumps(diagnosis), session_id)
        ) if hasattr(self.history, "conn") else None

        log.info(f"Rootcause analysis complete: {diagnosis.get('primary_cause')}")
        return diagnosis

    def _format_events(self, events: list) -> str:
        if not events:
            return "  (none)"
        lines = []
        for e in events:
            ts = datetime.fromtimestamp(e.ts).strftime("%m-%d %H:%M")
            line = f"  [{ts}] {e.type}"
            if e.sensor_id:
                line += f" {e.sensor_id}={e.value}"
            if e.note:
                line += f" — {e.note}"
            lines.append(line)
        return "\n".join(lines)

    def _format_decisions(self, decisions: list) -> str:
        if not decisions:
            return "  (none)"
        lines = []
        for d in decisions:
            ts = datetime.fromtimestamp(d.ts).strftime("%m-%d %H:%M")
            override = " [OVERRIDDEN]" if d.was_overridden else ""
            lines.append(
                f"  [{ts}]{override} {json.dumps(d.commands)} — {d.reason}"
            )
        return "\n".join(lines)
