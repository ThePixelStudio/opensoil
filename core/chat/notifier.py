"""
Notifier — broadcasts proactive alerts to all registered chat engines.

The orchestrator calls notifier.notify() when significant events happen
(sensor anomaly, safety override, LLM failure, no-effect detection).
Each registered engine pushes the message to its platform.
"""

import logging

log = logging.getLogger("opensoil.chat.notifier")


class Notifier:

    def __init__(self):
        self._engines: list = []

    def register(self, engine) -> None:
        """Register a chat engine to receive notifications."""
        self._engines.append(engine)
        log.info(f"Notifier: registered {engine.__class__.__name__}")

    def notify(self, text: str, chat_id: str = None) -> None:
        """Broadcast text to all registered engines."""
        for engine in self._engines:
            try:
                engine.send(text, chat_id=chat_id)
            except Exception as e:
                log.warning(f"Notification failed ({engine.__class__.__name__}): {e}")
