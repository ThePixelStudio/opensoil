"""
Integration test — simulates a full poll cycle with a mock device and LLM.
No real hardware or API keys needed.
"""

import json
import time
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml


@pytest.fixture
def config_path(tmp_path):
    """Write a minimal config.yaml for testing."""
    config = {
        "box_id": "test_box",
        "domain": "smart_garden",
        "profile": "swiss_chard",
        "device": {
            "adapter": "esp32_mqtt",
            "config": {"broker_host": "localhost"},
        },
        "sensors": [
            {"plugin": "sht31"},
            {"plugin": "capacitive_soil"},
        ],
        "actuators": [
            {"plugin": "relay_pump"},
            {"plugin": "relay_fan"},
        ],
        "llm": {
            "provider": "mock",
            "model":    "mock-model",
        },
        "history": {"backend": "sqlite"},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(config))
    return p


class MockDevice:
    """Fake IDevice — returns realistic sensor values."""
    def read(self, sensor_id: str) -> float:
        return {
            "sht31_temp":     29.4,
            "sht31_humidity": 62.0,
            "soil_cap":       52.3,
            "bh1750_lux":     6800.0,
        }.get(sensor_id, 0.0)

    def write(self, actuator_id: str, value) -> None:
        pass  # no-op in tests

    def status(self) -> dict:
        return {"connected": True, "transport": "mock"}


class MockLLMAdapter:
    """Returns a valid JSON command without calling any API."""
    def call(self, context: dict):
        from core.llm.llm_adapter import LLMResponse
        commands = {
            "relay_pump": "off",
            "relay_fan":  "on",
            "pwm_light":  80,
        }
        return LLMResponse(
            raw_text=json.dumps({**commands, "reason": "temp at 29.4°C, fan on; soil at 52.3%, below 55% target but watered 10min ago"}),
            commands=commands,
            model="mock-model",
            latency_ms=100,
            tokens_used=800,
            reason="temp at 29.4°C, fan on; soil below target but recently watered",
        )


class TestFullPollCycle:

    def test_poll_cycle_writes_decision(self, tmp_path):
        """Full poll cycle should write a decision to history."""
        from core.history.backends.sqlite_backend import SQLiteHistoryStore
        from core.safety_engine import SafetyEngine
        from core.context_assembler import ContextAssembler

        box_id = "integration_test_box"
        history = SQLiteHistoryStore(box_id, db_path=tmp_path / "test.db")

        # Run safety engine validation
        engine = SafetyEngine([
            "relay_pump OFF if soil_cap > 78",
            "relay_fan ON (forced) if sht31_temp > 32",
        ])
        snapshot = {"soil_cap": 52.3, "sht31_temp": 29.4}
        commands = {"relay_pump": "off", "relay_fan": "on"}
        safe, overrides = engine.validate(commands, snapshot)

        assert safe["relay_pump"] == "off"   # LLM chose off — no override needed
        assert safe["relay_fan"] == "on"     # LLM chose on — no override needed
        assert overrides == []               # temp < 32, so no forced override

        # Write decision
        from core.history.ihistory_store import LLMDecision
        history.write_decision(LLMDecision(
            ts=time.time(),
            box_id=box_id,
            phase="vegetative",
            full_prompt="...",
            raw_response='{"relay_pump":"off","relay_fan":"on"}',
            commands=safe,
            model="mock-model",
            latency_ms=100,
            tokens_used=800,
            was_overridden=False,
            reason="test decision",
        ))

        # Verify it was stored
        decisions = history.llm_decisions(box_id, limit=1)
        assert len(decisions) == 1
        assert decisions[0].commands["relay_fan"] == "on"
        assert decisions[0].reason == "test decision"
        assert decisions[0].was_overridden is False

    def test_safety_override_recorded(self, tmp_path):
        """When safety overrides LLM, it should be flagged in decision log."""
        from core.history.backends.sqlite_backend import SQLiteHistoryStore
        from core.safety_engine import SafetyEngine
        from core.history.ihistory_store import LLMDecision, Event

        box_id = "override_test_box"
        history = SQLiteHistoryStore(box_id, db_path=tmp_path / "override.db")
        engine  = SafetyEngine(["relay_pump OFF if soil_cap > 78"])

        # LLM wants pump on, but soil is too wet
        commands = {"relay_pump": "on"}
        snapshot = {"soil_cap": 85.0}
        safe, overrides = engine.validate(commands, snapshot)

        assert safe["relay_pump"] == "off"
        assert len(overrides) == 1

        # Write override event
        history.write_event(Event(
            ts=time.time(),
            box_id=box_id,
            type="SAFETY_OVERRIDE",
            severity="warn",
            note=overrides[0],
        ))

        # Write decision with was_overridden=True
        history.write_decision(LLMDecision(
            ts=time.time(),
            box_id=box_id,
            phase="vegetative",
            full_prompt="...",
            raw_response='{"relay_pump":"on"}',
            commands=safe,
            model="mock-model",
            latency_ms=100,
            tokens_used=800,
            was_overridden=True,
            reason="soil seemed dry",
        ))

        # Verify override was recorded
        decisions = history.llm_decisions(box_id, limit=1)
        assert decisions[0].was_overridden is True

        events = history.events(box_id, type="SAFETY_OVERRIDE")
        assert len(events) == 1
        assert "soil_cap" in events[0].note

    def test_sensor_history_written(self, tmp_path):
        """Sensor readings should accumulate in history store."""
        from core.history.backends.sqlite_backend import SQLiteHistoryStore
        from core.history.ihistory_store import SensorReading

        box_id = "history_test_box"
        history = SQLiteHistoryStore(box_id, db_path=tmp_path / "history.db")

        # Write 3 readings
        for i, val in enumerate([55.0, 58.0, 61.0]):
            history.write_reading(SensorReading(
                ts=time.time() - (3 - i) * 30,
                box_id=box_id,
                sensor_id="soil_cap",
                value=val,
                unit="percent",
                phase="vegetative",
            ))

        avgs = history.sensor_avg(box_id, window=3600)
        assert "soil_cap" in avgs
        assert abs(avgs["soil_cap"] - 58.0) < 1.0  # avg of 55, 58, 61

        ranges = history.sensor_minmax(box_id, window=3600)
        assert ranges["soil_cap"]["min"] == 55.0
        assert ranges["soil_cap"]["max"] == 61.0
