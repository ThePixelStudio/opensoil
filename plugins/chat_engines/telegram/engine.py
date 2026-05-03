"""
Telegram chat engine plugin for OpenSoil.

Runs the bot in a background daemon thread with its own asyncio event loop
so the main orchestrator poll loop is never blocked.

config.yaml section:
  chat:
    engine: telegram
    config:
      token: "123456:ABC-your-bot-token"
      allowed_chat_ids: [123456789]   # optional whitelist; omit to allow anyone
"""

import asyncio
import logging
import threading
from typing import Callable, Optional

from core.chat.ichat_engine import IChatEngine

log = logging.getLogger("opensoil.chat.telegram")

WELCOME = (
    "OpenSoil connected 🌱\n\n"
    "Send a message in one of three modes:\n"
    "  • Observation   → leaves look pale today\n"
    "  • Question      → why is the soil dry?\n"
    "  • Actuator cmd  → pump on  /  fan off\n\n"
    "You'll also receive alerts for anomalies and safety events."
)


class Engine(IChatEngine):

    def __init__(self, config: dict):
        self._token       = config["token"]
        self._allowed_ids = [int(x) for x in config.get("allowed_chat_ids", [])]
        self._active_chats: list[int] = []

        self._on_message: Optional[Callable[[str, str], str]] = None
        self._app         = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready       = threading.Event()

    # ── IChatEngine interface ─────────────────────────────────────────────────

    def start(self, on_message: Callable[[str, str], str]) -> None:
        self._on_message = on_message
        t = threading.Thread(target=self._run_loop, daemon=True, name="telegram-bot")
        t.start()
        if not self._ready.wait(timeout=15):
            log.error("Telegram bot did not become ready in 15s — check your token")
        else:
            log.info("Telegram bot ready")

    def send(self, text: str, chat_id: str = None) -> None:
        if not self._loop or not self._app:
            return
        targets = [int(chat_id)] if chat_id else list(self._active_chats)
        if not targets and self._allowed_ids:
            targets = [self._allowed_ids[0]]
        for cid in targets:
            asyncio.run_coroutine_threadsafe(
                self._safe_send(cid, text),
                self._loop,
            )

    def stop(self) -> None:
        if self._loop and self._app:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_bot())
        except Exception as e:
            log.error(f"Telegram bot crashed: {e}")

    async def _run_bot(self):
        from telegram import Update
        from telegram.ext import (
            Application, CommandHandler, MessageHandler,
            filters, ContextTypes,
        )

        self._app = Application.builder().token(self._token).build()

        async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            cid = update.effective_chat.id
            if not self._authorized(cid):
                await update.message.reply_text("Unauthorized.")
                return
            self._register_chat(cid)
            await update.message.reply_text(WELCOME)

        async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            cid  = update.effective_chat.id
            if not self._authorized(cid):
                return
            self._register_chat(cid)
            text  = update.message.text or ""
            reply = (
                self._on_message(str(cid), text)
                if self._on_message
                else "OpenSoil not ready yet."
            )
            await update.message.reply_text(reply)

        self._app.add_handler(CommandHandler("start", on_start))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_message)
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._ready.set()

        # Keep the event loop alive — stopped by stop()
        await asyncio.Event().wait()

    async def _safe_send(self, chat_id: int, text: str):
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            log.warning(f"Telegram send failed to {chat_id}: {e}")

    async def _shutdown(self):
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception as e:
            log.warning(f"Telegram shutdown error: {e}")

    def _authorized(self, chat_id: int) -> bool:
        return not self._allowed_ids or chat_id in self._allowed_ids

    def _register_chat(self, chat_id: int):
        if chat_id not in self._active_chats:
            self._active_chats.append(chat_id)
