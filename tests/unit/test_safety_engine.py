"""
Tests for SafetyEngine — the most critical component.
The safety engine is the last line of defense before physical actuation.
"""

import pytest
from core.safety_engine import SafetyEngine


class TestThresholdRules:

    def test_pump_blocked_when_soil_too_wet(self):
        engine = SafetyEngine(["relay_pump OFF if soil_cap > 78"])
        commands = {"relay_pump": "on"}
        safe, overrides = engine.validate(commands, {"soil_cap": 85.0})
        assert safe["relay_pump"] == "off"
        assert len(overrides) == 1
        assert "soil_cap" in overrides[0]

    def test_pump_allowed_when_soil_dry(self):
        engine = SafetyEngine(["relay_pump OFF if soil_cap > 78"])
        commands = {"relay_pump": "on"}
        safe, overrides = engine.validate(commands, {"soil_cap": 55.0})
        assert safe["relay_pump"] == "on"
        assert overrides == []

    def test_fan_forced_on_above_threshold(self):
        engine = SafetyEngine(["relay_fan ON (forced) if sht31_temp > 32"])
        commands = {"relay_fan": "off"}
        safe, overrides = engine.validate(commands, {"sht31_temp": 33.5})
        assert safe["relay_fan"] == "on"
        assert len(overrides) == 1

    def test_fan_not_forced_below_threshold(self):
        engine = SafetyEngine(["relay_fan ON (forced) if sht31_temp > 32"])
        commands = {"relay_fan": "off"}
        safe, overrides = engine.validate(commands, {"sht31_temp": 28.0})
        assert safe["relay_fan"] == "off"
        assert overrides == []

    def test_mist_blocked_high_humidity(self):
        engine = SafetyEngine(["mist_spray OFF if sht31_humidity > 82"])
        commands = {"mist_spray": "on"}
        safe, overrides = engine.validate(commands, {"sht31_humidity": 90.0})
        assert safe["mist_spray"] == "off"
        assert len(overrides) == 1

    def test_missing_sensor_skips_rule(self):
        """If the sensor is not in snapshot, rule must not crash."""
        engine = SafetyEngine(["relay_pump OFF if soil_cap > 78"])
        commands = {"relay_pump": "on"}
        safe, overrides = engine.validate(commands, {})  # no soil_cap
        assert safe["relay_pump"] == "on"   # rule skipped, not applied
        assert overrides == []


class TestTimeRules:

    def test_light_off_during_night(self, monkeypatch):
        from datetime import datetime
        # Mock time to 20:00 (night)
        class FakeDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 4, 25, 20, 0, 0)
        monkeypatch.setattr("core.safety_engine.datetime", FakeDateTime)

        engine = SafetyEngine(["pwm_light OFF between 18:00 and 06:00"])
        commands = {"pwm_light": "on"}
        safe, overrides = engine.validate(commands, {})
        assert safe["pwm_light"] == "off"
        assert len(overrides) == 1

    def test_light_on_during_day(self, monkeypatch):
        from datetime import datetime
        class FakeDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 4, 25, 10, 0, 0)
        monkeypatch.setattr("core.safety_engine.datetime", FakeDateTime)

        engine = SafetyEngine(["pwm_light OFF between 18:00 and 06:00"])
        commands = {"pwm_light": "on"}
        safe, overrides = engine.validate(commands, {})
        assert safe["pwm_light"] == "on"
        assert overrides == []


class TestMultipleRules:

    def test_multiple_rules_all_applied(self):
        engine = SafetyEngine([
            "relay_pump OFF if soil_cap > 78",
            "relay_fan ON (forced) if sht31_temp > 32",
            "mist_spray OFF if sht31_humidity > 82",
        ])
        commands = {
            "relay_pump": "on",
            "relay_fan": "off",
            "mist_spray": "on",
        }
        snapshot = {"soil_cap": 85.0, "sht31_temp": 35.0, "sht31_humidity": 90.0}
        safe, overrides = engine.validate(commands, snapshot)

        assert safe["relay_pump"] == "off"
        assert safe["relay_fan"] == "on"
        assert safe["mist_spray"] == "off"
        assert len(overrides) == 3

    def test_unparseable_rule_skipped_gracefully(self):
        """Bad rule string should not crash the engine."""
        engine = SafetyEngine([
            "relay_pump OFF if soil_cap > 78",
            "THIS IS NOT A VALID RULE !!!",
        ])
        commands = {"relay_pump": "on"}
        safe, overrides = engine.validate(commands, {"soil_cap": 85.0})
        # Valid rule still applied, invalid rule silently skipped
        assert safe["relay_pump"] == "off"
        assert len(overrides) == 1

    def test_no_rules_no_overrides(self):
        engine = SafetyEngine([])
        commands = {"relay_pump": "on", "relay_fan": "off"}
        safe, overrides = engine.validate(commands, {"soil_cap": 99.0})
        assert safe == commands
        assert overrides == []
