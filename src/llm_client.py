from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from src.observability import METRICS, log_event, timer
from src.types import SQLGenerationOutput, AnswerGenerationOutput

DEFAULT_MODEL = "openai/gpt-5-nano"
logger = logging.getLogger(__name__)


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Conservative fallback when provider usage is missing.
        # Rough heuristic: ~4 chars/token for English-ish text.
        if not text:
            return 0
        return max(1, int(len(text) / 4))

    @classmethod
    def _estimate_prompt_tokens(cls, messages: list[dict[str, str]]) -> int:
        joined = "\n".join(f'{m.get("role","")}:{m.get("content","")}' for m in (messages or []))
        return cls._estimate_tokens(joined)

    @staticmethod
    def _get_usage_field(usage: Any, key: str) -> int | None:
        if usage is None:
            return None
        if isinstance(usage, dict):
            val = usage.get(key)
            return val if isinstance(val, int) else None
        val = getattr(usage, key, None)
        return val if isinstance(val, int) else None

    def _record_usage(self, *, messages: list[dict[str, str]], completion_text: str, res: Any) -> None:
        self._stats["llm_calls"] = int(self._stats.get("llm_calls", 0)) + 1

        usage = getattr(res, "usage", None)
        prompt_tokens = self._get_usage_field(usage, "prompt_tokens")
        completion_tokens = self._get_usage_field(usage, "completion_tokens")
        total_tokens = self._get_usage_field(usage, "total_tokens")

        if prompt_tokens is None:
            prompt_tokens = self._estimate_prompt_tokens(messages)
        if completion_tokens is None:
            completion_tokens = self._estimate_tokens(completion_text)
        if total_tokens is None:
            total_tokens = int(prompt_tokens) + int(completion_tokens)

        self._stats["prompt_tokens"] = int(self._stats.get("prompt_tokens", 0)) + int(prompt_tokens)
        self._stats["completion_tokens"] = int(self._stats.get("completion_tokens", 0)) + int(completion_tokens)
        self._stats["total_tokens"] = int(self._stats.get("total_tokens", 0)) + int(total_tokens)

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        start = time.perf_counter()
        with timer("llm_chat_ms"):
            res = self._client.chat.send(
                messages=messages,
                model=self.model,
                temperature=temperature,
                # max_tokens=max_tokens,
                stream=False,
            )

        choices = getattr(res, "choices", None) or []
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter response content is not text.")
        content = content.strip()
        self._record_usage(messages=messages, completion_text=content, res=res)
        dur_ms = (time.perf_counter() - start) * 1000
        log_event(logger, "llm_chat", model=self.model, duration_ms=dur_ms, llm_stats=dict(self._stats))
        return content

    @staticmethod
    def _extract_sql(text: str) -> str | None:
        maybe_json = text.strip()
        if maybe_json.startswith("{") and maybe_json.endswith("}"):
            try:
                parsed = json.loads(maybe_json)
                sql = parsed.get("sql")
                if isinstance(sql, str) and sql.strip():
                    return sql.strip()
                return None
            except json.JSONDecodeError:
                pass
        lower = text.lower()
        idx = min(
            lower.find("select ") if lower.find("select ") >= 0 else len(lower),
            lower.find("with ") if lower.find("with ") >= 0 else len(lower),
        )

        if idx < len(lower):
            return text[idx:].strip()
        return None

    def generate_sql(self, question: str, context: dict[str, Any]) -> SQLGenerationOutput:
        system_prompt = (
            "You are a SQL assistant. "
            "Generate SQLite SELECT queries from natural language questions. "
            "Use only table and column names from the provided database schema. "
        )

        # Prefer a compact column list over full schema objects.
        columns_val = context.get("columns")
        columns: list[str] = []
        if isinstance(columns_val, list):
            columns = [c for c in columns_val if isinstance(c, str)]

        table = context.get("table")
        rest = {k: v for k, v in context.items() if k not in ("columns", "table")}

        blocks: list[str] = []
        if columns:
            blocks.append(f"Columns:\n{', '.join(columns)}")
        if isinstance(table, str) and table.strip():
            blocks.append(f"Table: {table.strip()}")
        if rest:
            blocks.append(f"Additional context:\n{json.dumps(rest, ensure_ascii=True)}")
        blocks.append(f"Question:\n{question}")
        blocks.append(
            "Generate a single SQLite SELECT query to answer the question. "
            "Give me the final SQL clearly without any other text. "
            "If you cannot answer the question, return 'I cannot answer this question.' without any other text."
        )
        user_prompt = "\n\n".join(blocks)

        start = time.perf_counter()
        error = None
        sql = None

        approx_tokens = (len(user_prompt) + len(system_prompt)) / 4

        try:
            text = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.0,
                max_tokens=approx_tokens * 2,
            )
            sql = self._extract_sql(text)
        except Exception as exc:
            error = str(exc)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return SQLGenerationOutput(
            sql=sql,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def generate_answer(self, question: str, sql: str | None, rows: list[dict[str, Any]]) -> AnswerGenerationOutput:
        if not sql:
            return AnswerGenerationOutput(
                answer="I cannot answer this with the available table and schema. Please rephrase using known survey fields.",
                timing_ms=0.0,
                llm_stats={"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": self.model},
                error=None,
            )
        if not rows:
            return AnswerGenerationOutput(
                answer="Query executed, but no rows were returned.",
                timing_ms=0.0,
                llm_stats={"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": self.model},
                error=None,
            )

        system_prompt = (
            "You are a concise analytics assistant. "
            "Use only the provided SQL results. Do not invent data."
        )
        user_prompt = (
            f"Question:\n{question}\n\nSQL:\n{sql}\n\n"
            f"Rows (JSON):\n{json.dumps(rows[:30], ensure_ascii=True)}\n\n"
            "Write a concise answer in plain English."
        )

        start = time.perf_counter()
        error = None
        answer = ""

        try:
            answer = self._chat(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=220,
            )
        except Exception as exc:
            error = str(exc)
            answer = f"Error generating answer: {error}"

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats or {})
        self._stats = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return out


def build_default_llm_client() -> OpenRouterLLMClient:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return OpenRouterLLMClient(api_key=api_key)
