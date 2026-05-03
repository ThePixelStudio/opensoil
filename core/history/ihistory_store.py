"""
IHistoryStore — plugable storage backend interface.
Default implementation: SQLite (zero config, runs on Pi Zero).
Contributors can add InfluxDB, Prometheus, TimescaleDB, etc.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import time


@dataclass
class SensorReading:
    ts: float
    box_id: str
    sensor_id: str
    value: float
    unit: str
    phase: str


@dataclass
class Event:
    ts: float
    box_id: str
    type: str           # PLANT_ADDED, PHASE_CHANGE, SENSOR_ANOMALY, ACTUATOR_FIRED,
                        # SAFETY_OVERRIDE, PLANT_DIED, HARVEST, OBSERVATION, MANUAL_OVERRIDE
    severity: str       # info | warn | critical
    sensor_id: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    note: Optional[str] = None
    photo_path: Optional[str] = None


@dataclass
class LLMDecision:
    ts: float
    box_id: str
    phase: str
    full_prompt: str
    raw_response: str
    commands: dict
    model: str
    latency_ms: int
    tokens_used: int
    was_overridden: bool
    reason: str


@dataclass
class Session:
    id: str
    box_id: str
    domain_id: str
    profile_id: str
    started_at: float
    ended_at: Optional[float]
    outcome: Optional[str]      # success | died | harvested | active
    notes: Optional[str]
    rootcause: Optional[str]


class IHistoryStore(ABC):

    @abstractmethod
    def write_reading(self, reading: SensorReading) -> None:
        """Persist a single sensor reading."""
        ...

    @abstractmethod
    def write_event(self, event: Event) -> None:
        """Persist a lifecycle or anomaly event."""
        ...

    @abstractmethod
    def write_decision(self, decision: LLMDecision) -> None:
        """Persist a full LLM decision record."""
        ...

    @abstractmethod
    def sensor_avg(self, box_id: str, window: int) -> dict:
        """Return {sensor_id: avg_value} over last `window` seconds."""
        ...

    @abstractmethod
    def sensor_minmax(self, box_id: str, window: int) -> dict:
        """Return {sensor_id: {min, max}} over last `window` seconds."""
        ...

    @abstractmethod
    def sensor_trend(self, box_id: str, window: int) -> dict:
        """Return {sensor_id: slope_per_day} linear regression over window."""
        ...

    @abstractmethod
    def events(self, box_id: str, type: Optional[str] = None,
               since: Optional[float] = None) -> list[Event]:
        """Query events, optionally filtered by type and time."""
        ...

    @abstractmethod
    def llm_decisions(self, box_id: str, limit: int = 5) -> list[LLMDecision]:
        """Return last N LLM decisions, newest first."""
        ...

    @abstractmethod
    def create_session(self, session: Session) -> str:
        """Create a new grow/occupancy session. Returns session id."""
        ...

    @abstractmethod
    def close_session(self, session_id: str, outcome: str,
                      notes: Optional[str] = None) -> None:
        """Mark a session as ended with outcome."""
        ...
