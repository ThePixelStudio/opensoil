"""
LLMAdapter — provider-agnostic LLM interface.
Inherited from OpenClaw's provider plugin pattern.

Supported providers:
  claude   — Anthropic Claude API
  openai   — OpenAI-compatible API
  ollama   — Local Ollama (zero cost, private)
  gemini   — Google Gemini REST API

Config (from config.yaml):
  llm:
    provider: gemini
    model: gemini-2.0-flash
    api_key: AIza...
    call_interval_sec: 60
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("opensoil.llm_adapter")

MAX_RETRIES = 3
RETRY_DELAY = 5   # seconds


@dataclass
class LLMResponse:
    raw_text: str
    commands: dict
    model: str
    latency_ms: int
    tokens_used: int
    reason: str


class LLMAdapter:

    def __init__(self, config: dict):
        self.provider = config.get("provider", "claude")
        self.model    = config.get("model", "claude-sonnet-4-6")
        self.api_key  = config.get("api_key") or self._get_env_key()
        self.max_tokens = config.get("max_tokens", 1024)

    def call_freetext(self, context: dict) -> str:
        """Call LLM without JSON constraints — returns raw text. Used by ReflectionEngine."""
        for attempt in range(MAX_RETRIES):
            try:
                return self._call_freetext_once(context)
            except Exception as e:
                if "429" in str(e):
                    raise
                log.warning(f"Freetext LLM call attempt {attempt+1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        raise RuntimeError(f"Freetext LLM call failed after {MAX_RETRIES} attempts")

    def _call_freetext_once(self, context: dict) -> str:
        if self.provider == "claude":
            text, _ = self._call_claude(context)
        elif self.provider in ("openai", "openai_compatible"):
            text, _ = self._call_openai(context)
        elif self.provider == "ollama":
            text, _ = self._call_ollama(context, json_mode=False)
        elif self.provider == "gemini":
            text, _ = self._call_gemini(context, json_mode=False)
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")
        return text

    def call(self, context: dict) -> LLMResponse:
        """
        Send assembled context to the LLM and return parsed response.
        Retries up to MAX_RETRIES on transient failures.
        429 rate-limit errors are NOT retried — the poll interval provides
        the natural back-off so we don't burn quota on repeated attempts.
        On retry, a strict JSON reminder is appended to break markdown reasoning loops.
        """
        JSON_REMINDER = (
            "\n\nCRITICAL: your entire response must be a single JSON object. "
            "No bullets, no markdown, no explanation. Start with { and end with }."
        )
        for attempt in range(MAX_RETRIES):
            try:
                ctx = context if attempt == 0 else {
                    **context,
                    "user": context["user"] + JSON_REMINDER,
                }
                return self._call_once(ctx)
            except Exception as e:
                if "429" in str(e):
                    log.warning("LLM rate limited (429) — skipping retries, will try next poll cycle")
                    raise
                log.warning(f"LLM call attempt {attempt+1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts")

    def _call_once(self, context: dict) -> LLMResponse:
        t0 = time.time()

        if self.provider == "claude":
            raw, tokens = self._call_claude(context)
        elif self.provider in ("openai", "openai_compatible"):
            raw, tokens = self._call_openai(context)
        elif self.provider == "ollama":
            raw, tokens = self._call_ollama(context)
        elif self.provider == "gemini":
            raw, tokens = self._call_gemini(context)
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

        latency_ms = int((time.time() - t0) * 1000)
        commands   = self._parse_json(raw)
        reason     = commands.pop("reason", "")

        return LLMResponse(
            raw_text=raw,
            commands=commands,
            model=self.model,
            latency_ms=latency_ms,
            tokens_used=tokens,
            reason=reason,
        )

    # ── Provider implementations ─────────────────────────────────────────

    def _call_claude(self, context: dict) -> tuple[str, int]:
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key)
        msg = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            cache_control={"type": "ephemeral"},
            system=[
                {
                    "type": "text",
                    "text": context["system"],
                }
            ],
            messages=[{"role": "user", "content": context["user"]}],
        )
        cache_write = getattr(msg.usage, "cache_creation_input_tokens", 0) or 0
        cache_read  = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        if cache_read:
            log.debug(f"Cache HIT — {cache_read} tokens served from cache")
        elif cache_write:
            log.debug(f"Cache WRITE — {cache_write} tokens written to cache")
        tokens = msg.usage.input_tokens + msg.usage.output_tokens + cache_write
        return msg.content[0].text, tokens

    def _call_openai(self, context: dict) -> tuple[str, int]:
        import openai
        client = openai.OpenAI(api_key=self.api_key)
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": context["system"]},
                {"role": "user",   "content": context["user"]},
            ],
        )
        text   = resp.choices[0].message.content
        tokens = resp.usage.total_tokens
        return text, tokens

    def _call_ollama(self, context: dict, json_mode: bool = True) -> tuple[str, int]:
        import requests
        base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        body: dict = {
            "model":  self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": context["system"]},
                {"role": "user",   "content": context["user"]},
            ],
        }
        if json_mode:
            body["format"] = "json"
        resp = requests.post(
            f"{base_url}/api/chat",
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        data   = resp.json()
        text   = data["message"]["content"]
        tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)
        return text, tokens

    def _call_gemini(self, context: dict, json_mode: bool = True) -> tuple[str, int]:
        import requests
        url = (
            f"https://generativelanguage.googleapis.com/v1beta"
            f"/models/{self.model}:generateContent"
        )
        gen_config: dict = {"maxOutputTokens": self.max_tokens}
        if json_mode:
            gen_config["responseMimeType"] = "application/json"
        body = {
            "system_instruction": {"parts": [{"text": context["system"]}]},
            "contents": [{"parts": [{"text": context["user"]}]}],
            "generationConfig": gen_config,
        }
        resp = requests.post(
            url,
            params={"key": self.api_key},
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError(f"Gemini returned no candidates. Full response: {data}")

        candidate  = candidates[0]
        finish     = candidate.get("finishReason", "UNKNOWN")
        parts      = candidate.get("content", {}).get("parts", [])

        if not parts or not parts[0].get("text"):
            raise ValueError(
                f"Gemini returned empty content (finishReason={finish}). "
                f"Full response: {data}"
            )

        text   = parts[0]["text"]
        tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
        log.debug(f"Gemini raw response (finishReason={finish}): {text[:300]}")
        return text, tokens

    # ── JSON parsing ──────────────────────────────────────────────────────

    def _parse_json(self, raw: str) -> dict:
        """Extract first complete JSON object from LLM response."""
        log.info(f"Parsing LLM response for JSON content (truncated): {raw}")
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
        start = text.find("{")
        if start == -1:
            raise ValueError(f"No JSON object found in LLM response: {raw[:200]}")
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
            return obj
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parse failed: {e} — raw: {raw[:200]}")

    def _get_env_key(self) -> Optional[str]:
        return (
            os.getenv("ANTHROPIC_API_KEY") or
            os.getenv("OPENAI_API_KEY") or
            os.getenv("GOOGLE_API_KEY") or
            os.getenv("OPENSOIL_LLM_KEY")
        )
