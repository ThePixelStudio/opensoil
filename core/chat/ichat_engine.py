"""
IChatEngine — interface every external chat engine plugin must implement.

The engine receives inbound messages and can send proactive notifications.
It runs in a background thread and never blocks the main poll loop.
"""

from abc import ABC, abstractmethod
from typing import Callable


class IChatEngine(ABC):

    @abstractmethod
    def start(self, on_message: Callable[[str, str], str]) -> None:
        """
        Start listening for inbound messages.

        on_message(sender_id, text) -> reply
          sender_id  — platform-specific identifier (Telegram chat_id, etc.)
          text       — raw message text from the user
          return     — reply string to send back immediately
        """

    @abstractmethod
    def send(self, text: str, chat_id: str = None) -> None:
        """
        Send a proactive message (anomaly alert, safety event, etc.).

        chat_id — specific recipient; if None, broadcast to all active chats.
        """

    @abstractmethod
    def stop(self) -> None:
        """Graceful shutdown — called on SIGINT / SIGTERM."""
