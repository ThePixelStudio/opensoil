"""
SafetyEngine — hard physical interlocks that the LLM cannot override.
Validates every LLM command before any actuator fires.
Logs every override as a SAFETY_OVERRIDE event for rootcause analysis.

Rule format (from domain.yaml):
  - "water_pump OFF if soil_cap > 78"
  - "fan ON (forced) if sht31_temp > 32"
  - "grow_light OFF between 18:00 and 06:00"
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger("opensoil.safety")


class SafetyEngine:

    def __init__(self, rules: list[str]):
        self.rules = rules
        self._parsed = [self._parse(r) for r in rules if r]

    def validate(self, commands: dict, snapshot: dict) -> tuple[dict, list[str]]:
        """
        Validate LLM commands against safety rules given current sensor snapshot.
        Returns (safe_commands, list_of_overrides_applied).
        Overrides are logged — critical for rootcause analysis.
        """
        safe = dict(commands)
        overrides = []

        for rule in self._parsed:
            if rule is None:
                continue
            override = rule(safe, snapshot)
            if override:
                overrides.append(override)
                log.warning(f"SAFETY OVERRIDE: {override}")

        return safe, overrides

    # ── Rule parser ──────────────────────────────────────────────────────────

    def _parse(self, rule_str: str):
        """
        Parses a rule string into a callable validator.
        Supports:
          "<actuator> OFF if <sensor> > <threshold>"
          "<actuator> OFF if <sensor> < <threshold>"
          "<actuator> ON (forced) if <sensor> > <threshold>"
          "<actuator> OFF between HH:MM and HH:MM"
          "<actuator> ON if <sensor> == <value>"
        """
        rule_str = rule_str.strip()

        # Time-based: "grow_light OFF between 18:00 and 06:00"
        m = re.match(
            r"(\w+)\s+(ON|OFF)\s+between\s+(\d+:\d+)\s+and\s+(\d+:\d+)",
            rule_str, re.IGNORECASE
        )
        if m:
            actuator, state, t_start, t_end = m.groups()
            return self._make_time_rule(actuator, state, t_start, t_end)

        # Threshold: "water_pump OFF if soil_cap > 78"
        m = re.match(
            r"(\w+)\s+(ON|OFF)(?:\s+\(forced\))?\s+if\s+(\w+)\s*([><=!]+)\s*([0-9.]+)",
            rule_str, re.IGNORECASE
        )
        if m:
            actuator, state, sensor, op, threshold = m.groups()
            forced = "(forced)" in rule_str.lower()
            return self._make_threshold_rule(
                actuator, state, sensor, op, float(threshold), forced
            )

        log.warning(f"Safety rule could not be parsed, skipping: '{rule_str}'")
        return None

    def _make_threshold_rule(self, actuator, state, sensor, op, threshold, forced):
        def rule(commands: dict, snapshot: dict):
            val = snapshot.get(sensor)
            if val is None:
                return None  # sensor not present — skip rule
            triggered = {
                ">":  val >  threshold,
                "<":  val <  threshold,
                ">=": val >= threshold,
                "<=": val <= threshold,
                "==": val == threshold,
                "!=": val != threshold,
            }.get(op, False)

            if triggered:
                target = state.lower()          # "on" or "off"
                current = commands.get(actuator)
                if forced or current != target:
                    commands[actuator] = target
                    return (
                        f"{actuator} forced to {state.upper()} "
                        f"because {sensor}={val} {op} {threshold}"
                    )
            return None
        return rule

    def _make_time_rule(self, actuator, state, t_start, t_end):
        def rule(commands: dict, snapshot: dict):
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute

            def to_minutes(t):
                h, m = map(int, t.split(":"))
                return h * 60 + m

            start_m = to_minutes(t_start)
            end_m   = to_minutes(t_end)

            # Handle overnight ranges (e.g. 18:00 to 06:00)
            if start_m > end_m:
                in_range = current_minutes >= start_m or current_minutes < end_m
            else:
                in_range = start_m <= current_minutes < end_m

            if in_range and commands.get(actuator) != state.lower():
                commands[actuator] = state.lower()
                return (
                    f"{actuator} forced to {state.upper()} "
                    f"(time rule: between {t_start} and {t_end})"
                )
            return None
        return rule
