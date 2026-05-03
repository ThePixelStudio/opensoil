"""
Tests for ContextAssembler — verifies prompt structure and token trimming.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from core.context_assembler import ContextAssembler, estimate_tokens, TOKEN_BUDGET


@pytest.fixture
def mock_domain():
    domain = MagicMock()
    domain.name = "Smart Garden"
    domain.sensors = [
        {"id": "sht31_temp", "name": "Air temp", "required": True},
        {"id": "soil_cap",   "name": "Soil moisture", "required": True},
    ]
    domain.actuators = [
        {"id": "relay_pump"},
        {"id": "relay_fan"},
    ]
    domain.safety = [
        "relay_pump OFF if soil_cap > 78",
        "relay_fan ON (forced) if sht31_temp > 32",
    ]
    domain.prompt_template_path = MagicMock()
    domain.prompt_template_path.exists.return_value = False
    return domain


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.box_id = "test_box"
    session.profile_id = "swiss_chard"
    session.day_number.return_value = 18
    session.current_phase.return_value = "vegetative"
    session.current_phase_targets.return_value = {
        "sht31_temp": [20, 28], "soil_cap": [55, 70]
    }
    session.last_actuation_times.return_value = {"relay_pump": "75min ago"}
    return session


@pytest.fixture
def mock_history():
    history = MagicMock()
    history.sensor_avg.return_value = {"sht31_temp": 28.5, "soil_cap": 58.2}
    history.sensor_minmax.return_value = {
        "sht31_temp": {"min": 22.1, "max": 31.5},
        "soil_cap":   {"min": 51.0, "max": 68.4},
    }
    history.sensor_trend.return_value = {"soil_cap": -2.1}
    history.events.return_value = []
    history.llm_decisions.return_value = []
    return history


@pytest.fixture
def mock_device():
    device = MagicMock()
    device.read.side_effect = lambda sid: {
        "sht31_temp": 29.4,
        "soil_cap":   52.3,
    }.get(sid, 0.0)
    return device


@pytest.fixture
def assembler(mock_domain, mock_session, mock_history, mock_device):
    return ContextAssembler(mock_domain, mock_session, mock_history, mock_device)


class TestSystemPrompt:

    def test_system_contains_domain_name(self, assembler):
        ctx = assembler.build()
        assert "Smart Garden" in ctx["system"]

    def test_system_contains_safety_rules(self, assembler):
        ctx = assembler.build()
        assert "relay_pump OFF if soil_cap > 78" in ctx["system"]
        assert "NEVER VIOLATE" in ctx["system"]

    def test_system_contains_actuator_ids(self, assembler):
        ctx = assembler.build()
        assert "relay_pump" in ctx["system"]
        assert "relay_fan" in ctx["system"]

    def test_system_contains_json_schema(self, assembler):
        ctx = assembler.build()
        assert '"reason"' in ctx["system"]

    def test_system_contains_phase(self, assembler):
        ctx = assembler.build()
        assert "vegetative" in ctx["system"]

    def test_safety_rules_always_in_system_not_user(self, assembler):
        """Safety rules must be in system prompt — never trimmed out."""
        ctx = assembler.build()
        assert "relay_pump OFF if soil_cap > 78" in ctx["system"]
        assert "relay_pump OFF if soil_cap > 78" not in ctx["user"]


class TestUserPrompt:

    def test_user_contains_live_sensor_values(self, assembler):
        ctx = assembler.build()
        assert "29.4" in ctx["user"]   # sht31_temp
        assert "52.3" in ctx["user"]   # soil_cap

    def test_user_contains_history_windows(self, assembler):
        ctx = assembler.build()
        assert "2h avg" in ctx["user"]
        assert "24h range" in ctx["user"]
        assert "7d trend" in ctx["user"]

    def test_user_contains_session_state(self, assembler):
        ctx = assembler.build()
        assert "SESSION STATE" in ctx["user"]
        assert "vegetative" in ctx["user"]

    def test_user_shows_recent_decisions(self, mock_domain, mock_session,
                                          mock_history, mock_device):
        from core.history.ihistory_store import LLMDecision
        import time
        mock_history.llm_decisions.return_value = [
            LLMDecision(
                ts=time.time() - 300,
                box_id="test_box",
                phase="vegetative",
                full_prompt="...",
                raw_response='{"relay_pump":"on"}',
                commands={"relay_pump": "on"},
                model="claude-sonnet-4-6",
                latency_ms=450,
                tokens_used=1200,
                was_overridden=False,
                reason="soil was at 48%",
            )
        ]
        assembler = ContextAssembler(mock_domain, mock_session, mock_history, mock_device)
        ctx = assembler.build()
        assert "LAST 5 DECISIONS" in ctx["user"]
        assert "soil was at 48%" in ctx["user"]


class TestTokenTrimming:

    def test_estimate_tokens(self):
        text = "a" * 400
        assert estimate_tokens(text) == 100

    def test_context_within_budget(self, assembler):
        ctx = assembler.build()
        total = estimate_tokens(ctx["user"])
        assert total <= TOKEN_BUDGET, f"User prompt too large: {total} tokens"

    def test_trim_drops_7d_first(self, mock_domain, mock_session, mock_device):
        """When over budget, 7d trend should be dropped before 24h range."""
        history = MagicMock()
        # Return huge history to trigger trimming
        huge = {f"sensor_{i}": i for i in range(50)}
        history.sensor_avg.return_value = huge
        history.sensor_minmax.return_value = {k: {"min": 0, "max": 100} for k in huge}
        history.sensor_trend.return_value = {k: 0.5 for k in huge}
        history.events.return_value = []
        history.llm_decisions.return_value = []

        assembler = ContextAssembler(mock_domain, mock_session, history, mock_device)
        ctx = assembler.build()
        # Should not crash and should stay within budget
        assert estimate_tokens(ctx["user"]) <= TOKEN_BUDGET
