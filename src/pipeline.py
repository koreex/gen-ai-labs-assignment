from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from src.llm_client import OpenRouterLLMClient, build_default_llm_client
from src.observability import METRICS, configure_logging, log_event, timer
from src.schema import extract_sqlite_columns
from src.types import (
    SQLValidationOutput,
    SQLExecutionOutput,
    PipelineOutput,
)
from src.validation import AnswerQualityValidator, ResultValidator, build_fallback_answer


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"
DEFAULT_TABLE_NAME = "gaming_mental_health"
MAX_RETRIES = 3

configure_logging()
logger = logging.getLogger(__name__)


class SQLValidationError(Exception):
    pass


class SQLValidator:
    _DISALLOWED_KEYWORDS = {
        "delete",
        "drop",
        "insert",
        "update",
        "alter",
        "create",
        "replace",
        "truncate",
        "attach",
        "detach",
        "vacuum",
        "pragma",
        "reindex",
        "analyze",
    }

    @staticmethod
    def _normalize(sql: str) -> str:
        sql = sql.strip()
        if sql.endswith(";"):
            sql = sql[:-1].strip()
        return sql

    @classmethod
    def _looks_like_single_statement(cls, sql: str) -> bool:
        # Reject multiple statements separated by semicolons.
        return ";" not in sql

    @classmethod
    def _starts_with_select_or_with(cls, sql: str) -> bool:
        lowered = sql.lstrip().lower()
        return lowered.startswith("select") or lowered.startswith("with")

    @classmethod
    def _is_read_only(cls, sql: str) -> bool:
        lowered = sql.lower()
        for kw in cls._DISALLOWED_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                return False
        if re.search(r"\bsqlite_master\b", lowered) or re.search(r"\bsqlite_schema\b", lowered):
            return False
        return True

    @classmethod
    def _references_expected_table(cls, sql: str, expected_table: str) -> bool:
        lowered = sql.lower()
        table = expected_table.lower()
        # Allow quotes around table name; accept FROM/JOIN references.
        return bool(
            re.search(rf'\bfrom\s+("?{re.escape(table)}"?)\b', lowered)
            or re.search(rf'\bjoin\s+("?{re.escape(table)}"?)\b', lowered)
        )

    @classmethod
    def _compiles_in_sqlite(cls, sql: str, *, db_path: Path) -> str | None:
        """Validate syntax + names without running the query.

        Note: SQLite's `EXPLAIN QUERY PLAN` can be permissive about unresolved
        identifiers in some cases; preparing a `LIMIT 0` wrapper is a stricter
        compilation check while still returning zero rows.
        """
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute(f"SELECT * FROM ({sql}) LIMIT 0")
                cur.fetchmany(1)
        except Exception as exc:
            return str(exc)
        return None

    @classmethod
    def validate(cls, sql: str | None, *, db_path: Path, expected_table: str) -> SQLValidationOutput:
        start = time.perf_counter()

        if sql is None:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error="No SQL provided",
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        sql_norm = cls._normalize(sql)
        error: str | None = None

        if not sql_norm:
            error = "Empty SQL"
        elif not cls._looks_like_single_statement(sql_norm):
            error = "Multiple SQL statements are not allowed"
        elif not cls._starts_with_select_or_with(sql_norm):
            error = "Only SELECT queries are allowed"
        elif not cls._is_read_only(sql_norm):
            error = "Only read-only SQL is allowed"
        elif not cls._references_expected_table(sql_norm, expected_table):
            error = f"Query must reference table '{expected_table}'"
        else:
            compile_err = cls._compiles_in_sqlite(sql_norm, db_path=db_path)
            if compile_err is not None:
                # Treat schema/name/syntax errors as invalid SQL (fits README validation intent).
                error = f"SQL does not compile against DB schema: {compile_err}"

        if error is not None:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error=error,
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        return SQLValidationOutput(
            is_valid=True,
            validated_sql=sql_norm,
            error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )


class SQLiteExecutor:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def run(self, sql: str | None) -> SQLExecutionOutput:
        start = time.perf_counter()
        error = None
        rows = []
        row_count = 0

        if sql is None:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error=None,
            )

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(100)]
                row_count = len(rows)
        except Exception as exc:
            error = str(exc)
            rows = []
            row_count = 0

        return SQLExecutionOutput(
            rows=rows,
            row_count=row_count,
            timing_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )


class AnalyticsPipeline:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, llm_client: OpenRouterLLMClient | None = None) -> None:
        self.db_path = Path(db_path)
        self.llm = llm_client or build_default_llm_client()
        self.executor = SQLiteExecutor(self.db_path)
        with timer("schema_extract_ms"):
            self._columns = extract_sqlite_columns(self.db_path, table=DEFAULT_TABLE_NAME)

    def select_relevant_columns(self, question: str, columns: list[str]) -> list[str]:
        def tokenize(s: str) -> set[str]:
            # Split into "single words": alphanumerics separated by non-alnum/_.
            # Keep it deterministic and cheap.
            parts = re.split(r"[^a-z0-9]+", s.lower().replace("_", " "))
            return {p for p in parts if p}

        q_tokens = tokenize(question)
        if not q_tokens:
            return []

        def tokens_match(a: str, b: str) -> bool:
            return a in b or b in a

        selected: list[str] = []
        seen: set[str] = set()
        for col in columns:
            if not isinstance(col, str) or not col:
                continue
            col_tokens = tokenize(col)
            if any(tokens_match(qt, ct) for qt in q_tokens for ct in col_tokens):
                if col not in seen:
                    selected.append(col)
                    seen.add(col)
        return selected

    def run(self, question: str, request_id: str | None = None) -> PipelineOutput:
        start = time.perf_counter()
        METRICS.inc("pipeline_requests_total", 1)

        log_event(logger, "pipeline_start", question=question, request_id=request_id)

        for retry in range(MAX_RETRIES):
            # Stage 1: SQL Generation
            columns = self.select_relevant_columns(question, self._columns) if retry == 0 else self._columns
            with timer("stage_sql_generation_ms"):
                sql_gen_output = self.llm.generate_sql(
                    question,
                    {"columns": columns, "table": DEFAULT_TABLE_NAME, "request_id": request_id},
                )
            sql = sql_gen_output.sql
            METRICS.inc("llm_calls_total", int(sql_gen_output.llm_stats.get("llm_calls", 0)))

            # Stage 2: SQL Validation
            with timer("stage_sql_validation_ms"):
                validation_output = SQLValidator.validate(
                    sql,
                    db_path=self.db_path,
                    expected_table=DEFAULT_TABLE_NAME,
                )
            if not validation_output.is_valid:
                sql = None
                METRICS.inc("sql_invalid_total", 1)
            else:
                sql = validation_output.validated_sql
                break

        # Stage 3: SQL Execution
        with timer("stage_sql_execution_ms"):
            execution_output = self.executor.run(sql)
        rows = execution_output.rows

        if execution_output.error:
            METRICS.inc("sql_execution_error_total", 1)

        # Result validation (analytics context)
        result_validation = ResultValidator.validate(rows if sql else [])
        if not result_validation.is_valid:
            # Treat empty/invalid results as unanswerable for analytics.
            sql = None
            rows = []

        # Stage 4: Answer Generation
        with timer("stage_answer_generation_ms"):
            answer_output = self.llm.generate_answer(question, sql, rows)
        METRICS.inc("llm_calls_total", int(answer_output.llm_stats.get("llm_calls", 0)))

        # Answer quality validation + deterministic fallback (no extra LLM calls)
        aq = AnswerQualityValidator.validate(answer_output.answer, rows=rows, sql=sql)
        if not aq.is_valid:
            answer_output.answer = build_fallback_answer(question=question, sql=sql, rows=rows)
            answer_output.error = aq.error

        # Determine status
        status = "success"
        if sql_gen_output.sql is None and sql_gen_output.error:
            status = "unanswerable"
        elif not validation_output.is_valid:
            status = "invalid_sql"
        elif execution_output.error:
            status = "error"
        elif sql is None:
            status = "unanswerable"

        # Build timings aggregate
        timings = {
            "sql_generation_ms": sql_gen_output.timing_ms,
            "sql_validation_ms": validation_output.timing_ms,
            "sql_execution_ms": execution_output.timing_ms,
            "answer_generation_ms": answer_output.timing_ms,
            "total_ms": (time.perf_counter() - start) * 1000,
        }
        METRICS.observe_ms("pipeline_total_ms", timings["total_ms"])
        METRICS.inc(f"pipeline_status_total.{status}", 1)
        log_event(logger, "pipeline_end", status=status, timings=timings)

        # Build total LLM stats
        total_llm_stats = {
            "llm_calls": sql_gen_output.llm_stats.get("llm_calls", 0) + answer_output.llm_stats.get("llm_calls", 0),
            "prompt_tokens": sql_gen_output.llm_stats.get("prompt_tokens", 0) + answer_output.llm_stats.get("prompt_tokens", 0),
            "completion_tokens": sql_gen_output.llm_stats.get("completion_tokens", 0) + answer_output.llm_stats.get("completion_tokens", 0),
            "total_tokens": sql_gen_output.llm_stats.get("total_tokens", 0) + answer_output.llm_stats.get("total_tokens", 0),
            "model": sql_gen_output.llm_stats.get("model", "unknown"),
        }

        return PipelineOutput(
            status=status,
            question=question,
            request_id=request_id,
            sql_generation=sql_gen_output,
            sql_validation=validation_output,
            sql_execution=execution_output,
            answer_generation=answer_output,
            sql=sql,
            rows=rows,
            answer=answer_output.answer,
            timings=timings,
            total_llm_stats=total_llm_stats,
        )