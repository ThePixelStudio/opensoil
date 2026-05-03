"""
Tests for PluginLoader — validates discovery and schema enforcement.
"""

import pytest
import tempfile
from pathlib import Path

import yaml

from core.plugin_loader import PluginLoader


@pytest.fixture
def plugin_dir(tmp_path):
    """Create a minimal plugin directory structure for testing."""
    # Sensor plugin
    s = tmp_path / "sensors" / "test_sensor"
    s.mkdir(parents=True)
    (s / "sensor.yaml").write_text(yaml.dump({
        "id": "test_sensor",
        "name": "Test Sensor",
        "version": "1.0.0",
        "author": "tester",
        "topics": [{"key": "test_temp", "unit": "celsius", "mqtt": "sensors/test"}],
        "poll_interval_sec": 30,
    }))

    # Actuator plugin
    a = tmp_path / "actuators" / "test_actuator"
    a.mkdir(parents=True)
    (a / "actuator.yaml").write_text(yaml.dump({
        "id": "test_actuator",
        "name": "Test Actuator",
        "version": "1.0.0",
        "author": "tester",
        "commands": [{"key": "relay", "type": "on_off", "mqtt": "actuator/relay/set"}],
    }))

    # Domain plugin
    d = tmp_path / "domains" / "test_domain"
    (d / "prompts").mkdir(parents=True)
    (d / "prompts" / "base.md").write_text("# Test domain prompt")
    (d / "domain.yaml").write_text(yaml.dump({
        "id": "test_domain",
        "name": "Test Domain",
        "version": "1.0.0",
        "sensors": [{"id": "test_sensor", "required": True}],
        "actuators": [{"id": "test_actuator"}],
        "safety": ["relay OFF if test_temp > 50"],
        "poll": {"interval_sec": 60},
    }))

    return tmp_path


class TestSensorDiscovery:

    def test_sensor_loaded(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        assert "test_sensor" in loader.sensors

    def test_sensor_fields(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        s = loader.sensors["test_sensor"]
        assert s.id == "test_sensor"
        assert s.version == "1.0.0"
        assert s.author == "tester"
        assert s.poll_interval_sec == 30

    def test_sensor_missing_required_field_skipped(self, tmp_path):
        s = tmp_path / "sensors" / "bad_sensor"
        s.mkdir(parents=True)
        (s / "sensor.yaml").write_text(yaml.dump({
            "id": "bad_sensor",
            # missing: name, version, author, topics, poll_interval_sec
        }))
        loader = PluginLoader(tmp_path)
        loader.load_all()
        assert "bad_sensor" not in loader.sensors  # rejected, not crashed


class TestActuatorDiscovery:

    def test_actuator_loaded(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        assert "test_actuator" in loader.actuators

    def test_actuator_has_commands(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        a = loader.actuators["test_actuator"]
        assert len(a.commands) == 1
        assert a.commands[0]["key"] == "relay"


class TestDomainDiscovery:

    def test_domain_loaded(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        assert "test_domain" in loader.domains

    def test_domain_prompt_path(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        d = loader.domains["test_domain"]
        assert d.prompt_template_path.exists()
        assert "Test domain prompt" in d.prompt_template_path.read_text()

    def test_domain_safety_rules(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        d = loader.domains["test_domain"]
        assert len(d.safety) == 1
        assert "relay OFF if test_temp > 50" in d.safety


class TestGetHelpers:

    def test_get_sensor_not_found_raises(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        with pytest.raises(KeyError, match="nonexistent"):
            loader.get_sensor("nonexistent")

    def test_get_domain_found(self, plugin_dir):
        loader = PluginLoader(plugin_dir)
        loader.load_all()
        d = loader.get_domain("test_domain")
        assert d.id == "test_domain"
