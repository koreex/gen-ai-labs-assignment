"""Extract SQLite table columns for LLM context."""

from __future__ import annotations

import sqlite3
from pathlib import Path

def extract_sqlite_columns(db_path: str | Path, *, table: str) -> list[str]:
    """Return a list of column names for a specific table.

    This project’s dataset is a single-table SQLite DB; keeping the context as a plain
    column list makes prompts smaller and less error-prone.
    """
    path = Path(db_path)
    if not path.exists():
        return []

    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        safe = table.replace('"', '""')
        cur.execute(f'PRAGMA table_info("{safe}")')
        cols = cur.fetchall()

    # cid, name, type, notnull, dflt_value, pk
    return [cname for _cid, cname, _ctype, _notnull, _dflt, _pk in cols]
