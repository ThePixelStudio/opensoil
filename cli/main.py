"""
OpenSoil CLI — main entry point.

Usage:
  opensoil start [--config config.yaml]   Start the poll loop
  opensoil status                          Show box status + last decision
  opensoil analyze <session_id>           Run rootcause analysis
  opensoil observe "<note>"               Add observation to OBSERVATIONS.md
  opensoil history [--hours 24]           Show sensor history
  opensoil plugins                        List all loaded plugins
  opensoil validate [--config config.yaml] Validate config + plugins
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml


def cmd_start(args):
    from core.orchestrator import Orchestrator
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}")
        sys.exit(1)

    print(f"Starting OpenSoil with {config_path}")
    orch = Orchestrator(config_path)

    # Wire up external chat engine if configured
    cfg      = _load_config(args.config)
    chat_cfg = cfg.get("chat", {})
    engine_id = chat_cfg.get("engine")
    if engine_id:
        from core.chat.chat_processor import ChatProcessor
        from core.chat.notifier import Notifier

        engine_plugin = orch.loader.chat_engines.get(engine_id)
        if not engine_plugin or not engine_plugin.engine_class:
            print(f"Warning: chat engine '{engine_id}' not found — starting without chat")
        else:
            domain   = orch.session.domain
            domain_cfg_dict = {"actuators": domain.actuators, "sensors": domain.sensors}
            processor = ChatProcessor(cfg, orch.history, domain_cfg_dict, orch.session.profile)
            engine    = engine_plugin.engine_class(chat_cfg.get("config", {}))
            notifier  = Notifier()
            notifier.register(engine)
            engine.start(lambda sender, text: processor.process(text)["response"])
            orch.set_notifier(notifier)
            print(f"Chat engine '{engine_id}' started")

    orch.start()


def cmd_status(args):
    from core.history.backends.sqlite_backend import SQLiteHistoryStore
    config = _load_config(args.config)
    box_id = config["box_id"]
    history = SQLiteHistoryStore(box_id)

    decisions = history.llm_decisions(box_id, limit=1)
    if not decisions:
        print(f"Box: {box_id} — no decisions recorded yet")
        return

    d = decisions[0]
    ts = datetime.fromtimestamp(d.ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'─'*50}")
    print(f"  Box        : {box_id}")
    print(f"  Last poll  : {ts}")
    print(f"  Phase      : {d.phase}")
    print(f"  Commands   : {json.dumps(d.commands)}")
    print(f"  Reason     : {d.reason}")
    print(f"  LLM model  : {d.model} ({d.latency_ms}ms, {d.tokens_used} tokens)")
    print(f"  Overridden : {'YES ⚠️' if d.was_overridden else 'No'}")
    print(f"{'─'*50}\n")

    # Device status
    device_cfg = config.get("device", {})
    print(f"  Device: {device_cfg.get('adapter')} "
          f"@ {device_cfg.get('config', {}).get('broker_host', '?')}")


def cmd_analyze(args):
    from core.history.backends.sqlite_backend import SQLiteHistoryStore
    from core.llm.llm_adapter import LLMAdapter
    from core.rootcause_analyzer import RootcauseAnalyzer

    config = _load_config(args.config)
    box_id = config["box_id"]
    history = SQLiteHistoryStore(box_id)
    llm     = LLMAdapter(config.get("llm", {}))
    analyzer = RootcauseAnalyzer(history, llm)

    print(f"Running rootcause analysis for session: {args.session_id}")
    result = analyzer.analyze(box_id, args.session_id)
    print(json.dumps(result, indent=2))


def cmd_observe(args):
    config = _load_config(args.config)
    box_id = config["box_id"]
    obs_dir = Path.home() / ".opensoil" / box_id
    obs_dir.mkdir(parents=True, exist_ok=True)
    obs_file = obs_dir / "OBSERVATIONS.md"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{ts}] {args.note}\n"
    with open(obs_file, "a") as f:
        f.write(entry)
    print(f"Observation saved: {entry.strip()}")


def cmd_ui(args):
    import uvicorn
    from core.web.server import app, init
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}")
        sys.exit(1)
    init(config_path)
    print(f"OpenSoil UI → http://localhost:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


def cmd_sensors(args):
    from core.history.backends.sqlite_backend import SQLiteHistoryStore
    config = _load_config(args.config)
    box_id = config["box_id"]
    history = SQLiteHistoryStore(box_id)

    rows = history.conn.execute(
        """
        SELECT sensor_id, value, ts
        FROM sensor_readings
        WHERE box_id = ?
          AND ts = (
            SELECT MAX(ts) FROM sensor_readings s2
            WHERE s2.sensor_id = sensor_readings.sensor_id
              AND s2.box_id = sensor_readings.box_id
          )
        ORDER BY sensor_id
        """,
        (box_id,)
    ).fetchall()

    if not rows:
        print(f"No sensor data yet for box: {box_id}")
        print("  → Is the ESP8266 connected and publishing to MQTT?")
        return

    print(f"\nLast sensor readings — box: {box_id}")
    print(f"{'─'*50}")
    import time
    for r in rows:
        age = int(time.time() - r["ts"])
        age_str = f"{age}s ago" if age < 120 else f"{age//60}m ago"
        print(f"  {r['sensor_id']:<25} {r['value']:>8.2f}   ({age_str})")
    print(f"{'─'*50}\n")


def cmd_history(args):
    from core.history.backends.sqlite_backend import SQLiteHistoryStore
    config = _load_config(args.config)
    box_id = config["box_id"]
    history = SQLiteHistoryStore(box_id)

    window = int(args.hours) * 3600
    avgs   = history.sensor_avg(box_id, window=window)
    ranges = history.sensor_minmax(box_id, window=window)
    trends = history.sensor_trend(box_id, window=window * 7)

    print(f"\nSensor history — last {args.hours}h for box: {box_id}")
    print(f"{'─'*50}")
    for sid in avgs:
        r = ranges.get(sid, {})
        t = trends.get(sid, 0)
        trend_str = f"↑ {t:+.2f}/day" if t > 0 else f"↓ {t:+.2f}/day"
        print(
            f"  {sid:<25} avg={avgs[sid]:>7.1f}  "
            f"[{r.get('min','-'):>6.1f} – {r.get('max','-'):<6.1f}]  "
            f"{trend_str}"
        )
    print(f"{'─'*50}\n")


def cmd_plugins(args):
    from core.plugin_loader import PluginLoader
    loader = PluginLoader()
    loader.load_all()

    print(f"\n{'─'*50}")
    print(f"  Sensors ({len(loader.sensors)}):")
    for p in loader.sensors.values():
        print(f"    {p.id:<25} v{p.version}  by {p.author}")

    print(f"\n  Actuators ({len(loader.actuators)}):")
    for p in loader.actuators.values():
        print(f"    {p.id:<25} v{p.version}  by {p.author}")

    print(f"\n  Devices ({len(loader.devices)}):")
    for p in loader.devices.values():
        print(f"    {p.id:<25} v{p.version}  [{p.transport}]")

    print(f"\n  Domains ({len(loader.domains)}):")
    for p in loader.domains.values():
        print(f"    {p.id:<25} v{p.version}  by {p.author}")

    print(f"\n  History backends ({len(loader.backends)}):")
    for p in loader.backends.values():
        print(f"    {p.id}")

    print(f"\n  Chat engines ({len(loader.chat_engines)}):")
    for p in loader.chat_engines.values():
        print(f"    {p.id:<25} {p.name}")
    print(f"{'─'*50}\n")


def cmd_validate(args):
    from core.plugin_loader import PluginLoader
    import yaml

    config = _load_config(args.config)
    loader = PluginLoader()
    loader.load_all()

    errors = []

    # Check domain exists
    domain_id = config.get("domain")
    if domain_id not in loader.domains:
        errors.append(f"Domain '{domain_id}' not found in plugins/domains/")

    # Check device adapter exists
    adapter = config.get("device", {}).get("adapter")
    if adapter not in loader.devices:
        errors.append(f"Device adapter '{adapter}' not found in plugins/devices/")

    # Check each sensor plugin exists
    for s in config.get("sensors", []):
        pid = s["plugin"]
        if pid not in loader.sensors:
            errors.append(f"Sensor plugin '{pid}' not found in plugins/sensors/")

    # Check each actuator plugin exists
    for a in config.get("actuators", []):
        pid = a["plugin"]
        if pid not in loader.actuators:
            errors.append(f"Actuator plugin '{pid}' not found in plugins/actuators/")

    if errors:
        print(f"\n❌ Validation failed ({len(errors)} errors):")
        for e in errors:
            print(f"   • {e}")
        sys.exit(1)
    else:
        print(f"\n✅ Config valid — all {len(loader.sensors)} sensors, "
              f"{len(loader.actuators)} actuators, "
              f"domain '{domain_id}', device '{adapter}' found.")


def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    parser = argparse.ArgumentParser(
        prog="opensoil",
        description="OpenSoil — LLM-driven IoT platform"
    )
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start",    help="Start the poll loop")
    sub.add_parser("status",   help="Show box status + last LLM decision")
    sub.add_parser("sensors",  help="Show last reading for every sensor")
    p_ui = sub.add_parser("ui", help="Start the web dashboard")
    p_ui.add_argument("--port", type=int, default=7070, help="Port to listen on (default: 7070)")
    sub.add_parser("plugins",  help="List all loaded plugins")
    sub.add_parser("validate", help="Validate config and plugins")

    p_analyze = sub.add_parser("analyze", help="Run rootcause analysis on a session")
    p_analyze.add_argument("session_id", help="Session ID to analyze")

    p_observe = sub.add_parser("observe", help="Add an observation note")
    p_observe.add_argument("note", help="Observation text (e.g. 'Leaves yellowing')")

    p_history = sub.add_parser("history", help="Show sensor history")
    p_history.add_argument("--hours", default=24, help="Hours of history to show")

    args = parser.parse_args()

    commands = {
        "start":    cmd_start,
        "status":   cmd_status,
        "sensors":  cmd_sensors,
        "ui":       cmd_ui,
        "analyze":  cmd_analyze,
        "observe":  cmd_observe,
        "history":  cmd_history,
        "plugins":  cmd_plugins,
        "validate": cmd_validate,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
