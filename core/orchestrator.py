"""
Orchestrator — the main poll loop.

Every N seconds:
  1. Read all sensors via device.read()
  2. Run pending no-effect checks (did actuators actually work?)
  3. Assemble context (live + history)
  4. Call LLM  → on failure: all actuators off (safe state)
  5. Validate through safety engine
  6. Enforce duration limits (max on-time per hour + max continuous run)
  7. Actuate via device.write()
  8. Persist everything to history store
"""

import json
import logging
import signal
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from .plugin_loader import PluginLoader
from .context_assembler import ContextAssembler
from .safety_engine import SafetyEngine
from .llm.llm_adapter import LLMAdapter
from .history.ihistory_store import Event, LLMDecision, Session
from .history.backends.sqlite_backend import SQLiteHistoryStore
from .reflection_engine import ReflectionEngine

log = logging.getLogger("opensoil.orchestrator")


class SessionState:
    """Runtime state for one active grow/occupancy session."""

    def __init__(self, config: dict, domain, profile: dict, started_at: float = None):
        self.box_id     = config["box_id"]
        self.profile_id = config.get("profile", domain.id)
        self.domain     = domain
        self.profile    = profile
        self._start     = started_at or time.time()
        self._last_actuations: dict[str, float] = {}

        # Actuator state tracking for duration limits
        self._actuator_states:   dict[str, Any]               = {}
        self._actuator_on_since: dict[str, float]             = {}
        self._actuator_on_log:   dict[str, list[tuple]]       = {}

    # ── Phase / profile helpers ───────────────────────────────────────────────

    def day_number(self) -> int:
        return int((time.time() - self._start) / 86400)

    def current_phase(self) -> str:
        if not self.profile or "phases" not in self.profile:
            return "unknown"
        day = self.day_number()
        for phase_name, phase_data in self.profile["phases"].items():
            days = phase_data.get("days", [0, 9999])
            if days[0] <= day <= days[1]:
                return phase_name
        return list(self.profile["phases"].keys())[-1]

    def current_phase_targets(self) -> dict:
        if not self.profile or "phases" not in self.profile:
            return {}
        phase = self.current_phase()
        return self.profile["phases"].get(phase, {}).get("targets", {})

    # ── Actuation time tracking ───────────────────────────────────────────────

    def record_actuation(self, actuator_id: str):
        self._last_actuations[actuator_id] = time.time()

    def last_actuation_times(self) -> dict:
        now = time.time()
        return {
            k: f"{int((now - v) / 60)}min ago"
            for k, v in self._last_actuations.items()
        }

    def _is_on(self, value) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return value.lower() == "on"
        if isinstance(value, (int, float)):
            return value > 0
        return False

    def update_actuator_state(self, actuator_id: str, value: Any):
        """Track on/off transitions to support duration limit calculations."""
        was_on = self._is_on(self._actuator_states.get(actuator_id))
        is_on  = self._is_on(value)
        now    = time.time()

        if not was_on and is_on:
            self._actuator_on_since[actuator_id] = now
        elif was_on and not is_on:
            on_since = self._actuator_on_since.pop(actuator_id, now)
            self._actuator_on_log.setdefault(actuator_id, []).append((on_since, now))

        self._actuator_states[actuator_id] = value

    def continuous_on_time(self, actuator_id: str) -> float:
        """Seconds the actuator has been continuously on (0 if currently off)."""
        on_since = self._actuator_on_since.get(actuator_id)
        return time.time() - on_since if on_since else 0.0

    def on_time_in_window(self, actuator_id: str, window_sec: float) -> float:
        """Total seconds the actuator was ON within the last window_sec."""
        since = time.time() - window_sec
        total = 0.0
        for (start, end) in self._actuator_on_log.get(actuator_id, []):
            if end < since:
                continue
            total += min(end, time.time()) - max(start, since)
        # Count any ongoing run
        on_since = self._actuator_on_since.get(actuator_id)
        if on_since:
            total += time.time() - max(on_since, since)
        return total


class Orchestrator:

    def __init__(self, config_path: Path):
        self.config  = self._load_config(config_path)
        self.running = False
        self._pending_checks: list[dict] = []

        # ── Load plugins ──────────────────────────────────────────────────
        self.loader = PluginLoader()
        self.loader.load_all()

        box_id    = self.config["box_id"]
        domain_id = self.config["domain"]
        domain    = self.loader.get_domain(domain_id)

        self._actuator_limits: dict = domain.actuator_limits

        # ── Load device adapter (single-node or multi-node) ──────────────
        nodes_cfg = self.config.get("nodes")
        if nodes_cfg:
            self.device = self._build_device_router(nodes_cfg)
        else:
            device_cfg    = self.config.get("device", {})
            device_plugin = self.loader.get_device(device_cfg["adapter"])
            self.device   = device_plugin.adapter_class(device_cfg.get("config", {}))

        # ── Load sub-profile ──────────────────────────────────────────────
        profile_id = self.config.get("profile")
        profile    = {}
        if profile_id and domain.profiles_dir.exists():
            profile_path = domain.profiles_dir / f"{profile_id}.yaml"
            if profile_path.exists():
                with open(profile_path) as f:
                    profile = yaml.safe_load(f)

        # ── History backend ───────────────────────────────────────────────
        backend_id = self.config.get("history", {}).get("backend", "sqlite")
        if backend_id == "sqlite":
            self.history = SQLiteHistoryStore(box_id)
        else:
            backend_plugin = self.loader.backends.get(backend_id)
            if not backend_plugin or not backend_plugin.backend_class:
                raise ValueError(f"History backend '{backend_id}' not found")
            self.history = backend_plugin.backend_class(
                box_id, self.config["history"].get("config", {})
            )
        self.safety    = SafetyEngine(domain.safety)
        self.llm       = LLMAdapter(self.config.get("llm", {}))
        self._notifier = None   # set via set_notifier() after construction

        poll_interval    = domain.poll.get("interval_sec", 60)
        poll_interval    = self.config.get("poll_interval_sec", poll_interval)
        self.poll_interval = self.config.get("llm", {}).get("call_interval_sec", poll_interval)

        # ── Resume or create session ──────────────────────────────────────
        existing = self.history.conn.execute(
            "SELECT id, started_at FROM sessions "
            "WHERE box_id=? AND ended_at IS NULL "
            "ORDER BY started_at ASC LIMIT 1",
            (box_id,),
        ).fetchone()

        if existing:
            session_id      = existing["id"]
            session_started = existing["started_at"]
            day = int((time.time() - session_started) / 86400)
            log.info(f"Resuming session {session_id} (day {day})")
            self.history.write_event(Event(
                ts=time.time(), box_id=box_id,
                type="DOMAIN_STARTED", severity="info",
                note=f"OpenSoil restarted — resumed session {session_id} day {day}",
            ))
        else:
            session_id      = str(uuid.uuid4())[:8]
            session_started = time.time()
            self.history.create_session(Session(
                id=session_id, box_id=box_id,
                domain_id=domain_id, profile_id=profile_id or domain_id,
                started_at=session_started, ended_at=None,
                outcome=None, notes=None, rootcause=None,
            ))
            self.history.write_event(Event(
                ts=time.time(), box_id=box_id,
                type="DOMAIN_STARTED", severity="info",
                note=f"OpenSoil started — new session, domain={domain_id}, profile={profile_id}",
            ))

        self.session = SessionState(self.config, domain, profile, started_at=session_started)

        # ── Core components (need self.session set first) ─────────────────
        self.reflection = ReflectionEngine(
            history=self.history,
            session=self.session,
            llm=self.llm,
            domain=domain,
            profile=profile,
        )
        self.context_assembler = ContextAssembler(
            domain=domain,
            session_state=self.session,
            history_store=self.history,
            device=self.device,
            reflection_engine=self.reflection,
        )

        if nodes_cfg:
            node_ids = ", ".join(n["id"] for n in nodes_cfg)
            device_desc = f"multi-node [{node_ids}]"
        else:
            device_desc = device_cfg.get("adapter", "unknown")
        log.info(
            f"Orchestrator ready — box={box_id}, domain={domain_id}, "
            f"device={device_desc}, poll={self.poll_interval}s"
        )

    def set_notifier(self, notifier) -> None:
        """Register a Notifier to receive proactive alerts (anomalies, failures)."""
        self._notifier = notifier

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        log.info("Orchestrator starting poll loop...")
        while self.running:
            try:
                self._poll()
            except Exception as e:
                log.error(f"Poll cycle failed: {e}", exc_info=True)
                self.history.write_event(Event(
                    ts=time.time(), box_id=self.session.box_id,
                    type="POLL_ERROR", severity="warn", note=str(e),
                ))
            time.sleep(self.poll_interval)

    def _poll(self):
        box_id = self.session.box_id
        t0     = time.time()
        log.debug(f"Poll cycle starting — {datetime.now().strftime('%H:%M:%S')}")

        # ── 0. Maybe generate a new reflection (no-op if interval not elapsed)
        self.reflection.maybe_reflect()

        # ── 1. Read all sensors ───────────────────────────────────────────
        snapshot: dict = {}
        phase = self.session.current_phase()

        from .device_router import DeviceRouter
        if isinstance(self.device, DeviceRouter):
            sensor_ids = [s["id"] for s in self.session.domain.sensors]
            snapshot   = self.device.read_snapshot(sensor_ids)
            unit_map   = {s["id"]: s.get("unit", "") for s in self.session.domain.sensors}
            for sid, val in snapshot.items():
                # strip node prefix or _min/_max suffix to look up unit
                base_sid = sid
                for node_id, _, _ in self.device._nodes:
                    if sid.startswith(f"{node_id}_"):
                        base_sid = sid[len(node_id) + 1:]
                        break
                if base_sid.endswith(("_min", "_max")):
                    base_sid = base_sid[:-4]
                self.history.write_reading(
                    _reading({"ts": t0, "box_id": box_id, "sensor_id": sid,
                              "value": val, "unit": unit_map.get(base_sid, ""),
                              "phase": phase})
                )
                self._check_anomaly(sid, val, box_id)
        else:
            for sensor_def in self.session.domain.sensors:
                sid = sensor_def["id"]
                try:
                    val = self.device.read(sid)
                    snapshot[sid] = val
                    self.history.write_reading(
                        _reading({"ts": t0, "box_id": box_id, "sensor_id": sid,
                                  "value": val, "unit": sensor_def.get("unit", ""),
                                  "phase": phase})
                    )
                    self._check_anomaly(sid, val, box_id)
                except Exception as e:
                    log.warning(f"Sensor read failed: {sid} — {e}")

        if not snapshot:
            log.warning("No sensor data available — skipping LLM call until device connects")
            return

        # ── 2. Check whether previous actuations had any effect ───────────
        self._run_effect_checks(snapshot)

        # ── 3. Assemble context ───────────────────────────────────────────
        context = self.context_assembler.build()

        # ── 4. Call LLM — on ANY failure push all actuators to safe off ───
        try:
            response = self.llm.call(context)
        except Exception as e:
            log.error(f"LLM call failed: {e}")
            self._actuators_all_off(str(e))
            return

        # ── 5. Safety engine hard interlocks ─────────────────────────────
        safe_commands, overrides = self.safety.validate(response.commands, snapshot)

        # ── 6. Duration limit guards ──────────────────────────────────────
        duration_overrides = self._check_duration_limits(safe_commands)
        overrides.extend(duration_overrides)

        # ── 7. Fill in any actuator the LLM omitted ───────────────────────
        for a in self.session.domain.actuators:
            if a["id"] not in safe_commands:
                safe_commands[a["id"]] = 0 if a["id"].startswith("pwm_") else "off"
                log.warning(f"LLM omitted '{a['id']}' — defaulting to off")

        # ── 8. Log all overrides ──────────────────────────────────────────
        was_overridden = len(overrides) > 0
        for override in overrides:
            self.history.write_event(Event(
                ts=time.time(), box_id=box_id,
                type="SAFETY_OVERRIDE", severity="warn", note=override,
            ))

        # ── 9. Actuate ────────────────────────────────────────────────────
        for actuator_id, value in safe_commands.items():
            old_state = self.session._actuator_states.get(actuator_id)
            try:
                self.device.write(actuator_id, value)
                self.session.update_actuator_state(actuator_id, value)
                self.session.record_actuation(actuator_id)
                self.history.write_event(Event(
                    ts=time.time(), box_id=box_id,
                    type="ACTUATOR_FIRED", severity="info",
                    note=f"{actuator_id}={value}",
                ))
            except Exception as e:
                log.error(f"Actuator write failed: {actuator_id}={value} — {e}")
                continue

            # Schedule a no-effect check when actuator just turned ON
            just_turned_on = (
                self.session._is_on(value) and
                not self.session._is_on(old_state)
            )
            if just_turned_on:
                limits    = self._actuator_limits.get(actuator_id, {})
                check_cfg = limits.get("no_effect_check")
                if check_cfg:
                    baseline = snapshot.get(check_cfg["sensor"])
                    if baseline is not None:
                        self._schedule_effect_check(actuator_id, check_cfg, baseline)

        # ── 10. Persist full LLM decision ────────────────────────────────
        self.history.write_decision(LLMDecision(
            ts=time.time(), box_id=box_id, phase=phase,
            full_prompt=f"{context['system']}\n\n---\n\n{context['user']}",
            raw_response=response.raw_text,
            commands=safe_commands,
            model=response.model,
            latency_ms=response.latency_ms,
            tokens_used=response.tokens_used,
            was_overridden=was_overridden,
            reason=response.reason,
        ))

        elapsed = int((time.time() - t0) * 1000)
        log.info(
            f"Poll done in {elapsed}ms — "
            f"LLM: {response.latency_ms}ms, commands: {safe_commands}, "
            f"reason: {response.reason}"
            + (f", OVERRIDES: {overrides}" if overrides else "")
        )

    # ── Safety: LLM failure → all off ────────────────────────────────────────

    def _actuators_all_off(self, reason: str):
        """Push every actuator to its safe off state when the LLM is unavailable."""
        box_id = self.session.box_id
        log.warning(f"LLM unavailable — setting all actuators OFF. Reason: {reason}")

        # Classify 429 / 5xx so the event note is actionable
        if "429" in reason:
            note = f"LLM rate-limited (429) — all actuators set to safe OFF"
        elif any(c in reason for c in ("503", "502", "500", "timeout")):
            note = f"LLM service error — all actuators set to safe OFF"
        else:
            note = f"LLM call failed — all actuators set to safe OFF: {reason}"

        self.history.write_event(Event(
            ts=time.time(), box_id=box_id,
            type="LLM_FAILURE_SAFE_OFF", severity="warn", note=note,
        ))
        if self._notifier:
            self._notifier.notify(f"🛡️ {note}")

        for a in self.session.domain.actuators:
            aid        = a["id"]
            safe_value = 0 if aid.startswith("pwm_") else "off"
            try:
                self.device.write(aid, safe_value)
                self.session.update_actuator_state(aid, safe_value)
            except Exception as e:
                log.error(f"Safe-off write failed for {aid}: {e}")

    # ── Safety: duration limits ───────────────────────────────────────────────

    def _check_duration_limits(self, commands: dict) -> list[str]:
        """
        Enforce per-actuator on-time budgets before any actuation.
        Returns a list of override descriptions (for logging).
        """
        overrides = []
        for actuator_id, proposed in list(commands.items()):
            if not self.session._is_on(proposed):
                continue  # already off — no limit to check

            limits  = self._actuator_limits.get(actuator_id, {})
            box_id  = self.session.box_id

            # Max continuous run
            max_cont = limits.get("max_continuous_sec")
            if max_cont:
                cont = self.session.continuous_on_time(actuator_id)
                if cont >= max_cont:
                    commands[actuator_id] = "off"
                    msg = (
                        f"{actuator_id} forced OFF: "
                        f"continuous run {int(cont)}s ≥ limit {max_cont}s"
                    )
                    overrides.append(msg)
                    self.history.write_event(Event(
                        ts=time.time(), box_id=box_id,
                        type="DURATION_LIMIT", severity="warn", note=msg,
                    ))
                    continue  # skip hourly check — already forcing off

            # Max on-time per hour
            max_hour = limits.get("max_on_per_hour_sec")
            if max_hour:
                used = self.session.on_time_in_window(actuator_id, 3600)
                if used >= max_hour:
                    commands[actuator_id] = "off"
                    msg = (
                        f"{actuator_id} forced OFF: "
                        f"hourly budget {int(used)}s ≥ limit {max_hour}s"
                    )
                    overrides.append(msg)
                    self.history.write_event(Event(
                        ts=time.time(), box_id=box_id,
                        type="DURATION_LIMIT", severity="warn", note=msg,
                    ))

        return overrides

    # ── Safety: no-effect detection ───────────────────────────────────────────

    def _schedule_effect_check(self, actuator_id: str, check_cfg: dict, baseline: float):
        self._pending_checks.append({
            "actuator":    actuator_id,
            "sensor":      check_cfg["sensor"],
            "baseline":    baseline,
            "direction":   check_cfg.get("direction", "increase"),
            "min_change":  check_cfg.get("min_change", 3.0),
            "check_at":    time.time() + check_cfg.get("check_after_sec", 90),
            "auto_disable": check_cfg.get("auto_disable", True),
        })
        log.debug(
            f"Effect check scheduled for {actuator_id}: "
            f"expect {check_cfg['sensor']} to {check_cfg.get('direction','increase')} "
            f"by {check_cfg.get('min_change', 3.0)} in "
            f"{check_cfg.get('check_after_sec', 90)}s"
        )

    def _run_effect_checks(self, snapshot: dict):
        """Fire any pending no-effect checks whose timer has elapsed."""
        now         = time.time()
        still_pending = []

        for chk in self._pending_checks:
            if now < chk["check_at"]:
                still_pending.append(chk)
                continue

            current  = snapshot.get(chk["sensor"])
            if current is None:
                continue  # sensor not in snapshot yet — discard check

            change   = current - chk["baseline"]
            expected = chk["direction"]
            passed   = (
                (expected == "increase" and change >= chk["min_change"]) or
                (expected == "decrease" and change <= -chk["min_change"])
            )

            if not passed:
                msg = (
                    f"{chk['actuator']} ran but {chk['sensor']} "
                    f"only changed {change:+.1f} "
                    f"(expected {expected} ≥ {chk['min_change']}). "
                    f"Possible fault: empty reservoir, clogged pipe, failed pump, or sensor error."
                )
                log.warning(msg)
                self.history.write_event(Event(
                    ts=now, box_id=self.session.box_id,
                    type="ACTUATOR_NO_EFFECT", severity="warn",
                    sensor_id=chk["sensor"], value=current, note=msg,
                ))
                if self._notifier:
                    self._notifier.notify(f"⚠️ No effect: {msg}")
                if chk["auto_disable"]:
                    try:
                        safe_val = 0 if chk["actuator"].startswith("pwm_") else "off"
                        self.device.write(chk["actuator"], safe_val)
                        self.session.update_actuator_state(chk["actuator"], safe_val)
                        log.warning(
                            f"{chk['actuator']} auto-disabled after no-effect detection"
                        )
                    except Exception as e:
                        log.error(f"Auto-disable write failed for {chk['actuator']}: {e}")

        self._pending_checks = still_pending

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _check_anomaly(self, sensor_id: str, value: float, box_id: str):
        try:
            stats = self.history.sensor_minmax(box_id, window=86400)
            if sensor_id not in stats:
                return
            mn, mx = stats[sensor_id]["min"], stats[sensor_id]["max"]
            mid    = (mn + mx) / 2
            sigma  = (mx - mn) / 6
            if sigma > 0 and abs(value - mid) > 3 * sigma:
                note = f"{sensor_id}={value} outside 3σ window [{mn:.1f}, {mx:.1f}]"
                self.history.write_event(Event(
                    ts=time.time(), box_id=box_id,
                    type="SENSOR_ANOMALY", severity="warn",
                    sensor_id=sensor_id, value=value,
                    threshold=mid + 3 * sigma,
                    note=note,
                ))
                if self._notifier:
                    self._notifier.notify(f"🔴 Anomaly: {note}")
        except Exception:
            pass

    def _build_device_router(self, nodes_cfg: list) -> "DeviceRouter":
        from .device_router import DeviceRouter
        nodes = []
        for node in nodes_cfg:
            node_id    = node["id"]
            adapter_id = node.get("adapter", "esp32_mqtt")
            cfg        = node.get("config", {})
            cfg["node_id"] = node_id  # inject so adapter uses namespaced topics
            plugin     = self.loader.get_device(adapter_id)
            adapter    = plugin.adapter_class(cfg)
            actuator_ids = node.get("actuators", [])
            nodes.append((node_id, adapter, actuator_ids))
            log.info(f"Multi-node: loaded node '{node_id}' ({adapter_id})")
        return DeviceRouter(nodes)

    def _shutdown(self, sig, frame):
        log.info("Shutting down gracefully...")
        self.running = False

    def _load_config(self, path: Path) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)


def _reading(d: dict):
    from .history.ihistory_store import SensorReading
    return SensorReading(**d)
