"""
IDevice — Hardware Abstraction Layer interface.
Every device adapter plugin must implement this.
The orchestrator ONLY calls these 3 methods — never anything hardware-specific.
"""

from abc import ABC, abstractmethod
from typing import Any


class IDevice(ABC):

    @abstractmethod
    def read(self, sensor_id: str) -> float:
        """
        Read a sensor value by its plugin id.
        Returns float. Raises SensorReadError if unavailable.
        """
        ...

    @abstractmethod
    def write(self, actuator_id: str, value: Any) -> None:
        """
        Send a command to an actuator by its plugin id.
        value: bool | int | float depending on actuator type.
        Raises ActuatorWriteError if device unreachable.
        """
        ...

    @abstractmethod
    def status(self) -> dict:
        """
        Return device health snapshot.
        Must include: connected (bool), transport (str), uptime_sec (int).
        """
        ...


class SensorReadError(Exception):
    pass


class ActuatorWriteError(Exception):
    pass
