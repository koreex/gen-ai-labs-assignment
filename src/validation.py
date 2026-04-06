from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    error: str | None = None
    warnings: list[str] | None = None


class ResultValidator:
    """Validates the SQL execution result for analytics suitability."""

    @classmethod
    def validate(cls, rows: list[dict[str, Any]] | None) -> ValidationResult:
        if rows is None:
            return ValidationResult(is_valid=False, error="No rows object returned")
        if not isinstance(rows, list):
            return ValidationResult(is_valid=False, error="Rows must be a list")
        if len(rows) == 0:
            return ValidationResult(is_valid=False, error="Query returned no rows")

        warnings: list[str] = []
        for i, row in enumerate(rows[:5]):
            if not isinstance(row, dict):
                return ValidationResult(is_valid=False, error=f"Row {i} is not an object")
            for k in row.keys():
                if not isinstance(k, str):
                    return ValidationResult(is_valid=False, error=f"Row {i} has non-string key")

        # Common analytics smell: too many columns -> likely SELECT *
        if isinstance(rows[0], dict) and len(rows[0].keys()) > 15:
            warnings.append("Result has many columns; consider selecting only needed fields")

        return ValidationResult(is_valid=True, warnings=warnings or None)


class AnswerQualityValidator:
    """Heuristic checks to avoid empty / contradictory / error answers."""

    @classmethod
    def validate(cls, answer: str | None, *, rows: list[dict[str, Any]] | None, sql: str | None) -> ValidationResult:
        if not isinstance(answer, str) or not answer.strip():
            return ValidationResult(is_valid=False, error="Empty answer")
        a = answer.strip().lower()
        if "error generating answer" in a:
            return ValidationResult(is_valid=False, error="LLM answer generation error string detected")
        if "cannot answer" in a and rows and sql:
            return ValidationResult(is_valid=False, error="Answer claims unanswerable despite having SQL + rows")
        return ValidationResult(is_valid=True)


def build_fallback_answer(*, question: str, sql: str | None, rows: list[dict[str, Any]] | None, max_rows: int = 5) -> str:
    """Deterministic, non-hallucinated fallback answer derived from returned rows."""
    if not sql:
        return "I cannot answer this with the available table and schema. Please rephrase using known survey fields."
    if not rows:
        return "Query executed, but no rows were returned."

    preview = rows[: max_rows if max_rows > 0 else 5]
    lines: list[str] = ["Based on the SQL results:"]
    for r in preview:
        # Keep it compact and robust.
        parts = [f"{k}={r.get(k)!r}" for k in sorted(r.keys())[:8]]
        lines.append("- " + ", ".join(parts))
    if len(rows) > len(preview):
        lines.append(f"(showing {len(preview)} of {len(rows)} rows)")
    return "\n".join(lines)
