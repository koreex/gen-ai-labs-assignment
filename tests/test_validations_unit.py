from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.pipeline import SQLValidator
from src.validation import AnswerQualityValidator, ResultValidator


class SQLValidatorUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmpdir.name) / "test.sqlite"
        self.table = "gaming_mental_health"
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(f'CREATE TABLE "{self.table}" (age INTEGER, gender TEXT, addiction_level REAL)')
            cur.execute(f'INSERT INTO "{self.table}" (age, gender, addiction_level) VALUES (25, "male", 4.0)')
            conn.commit()

    def tearDown(self) -> None:
        try:
            self.tmpdir.cleanup()
        except PermissionError:
            pass

    def test_valid_select_compiles(self) -> None:
        out = SQLValidator.validate(
            'SELECT gender, AVG(addiction_level) AS avg_add FROM "gaming_mental_health" GROUP BY gender',
            db_path=self.db_path,
            expected_table=self.table,
        )
        self.assertTrue(out.is_valid)
        self.assertIsNotNone(out.validated_sql)

    def test_rejects_non_select(self) -> None:
        out = SQLValidator.validate(
            'DELETE FROM "gaming_mental_health"',
            db_path=self.db_path,
            expected_table=self.table,
        )
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_missing_expected_table_reference(self) -> None:
        out = SQLValidator.validate(
            "SELECT 1",
            db_path=self.db_path,
            expected_table=self.table,
        )
        self.assertFalse(out.is_valid)
        self.assertIn("must reference", (out.error or "").lower())

    def test_rejects_unknown_column_via_compile(self) -> None:
        out = SQLValidator.validate(
            'SELECT not_a_column FROM "gaming_mental_health"',
            db_path=self.db_path,
            expected_table=self.table,
        )
        self.assertFalse(out.is_valid)
        self.assertIn("does not compile", (out.error or "").lower())


class ResultValidatorUnitTests(unittest.TestCase):
    def test_rejects_none(self) -> None:
        out = ResultValidator.validate(None)
        self.assertFalse(out.is_valid)

    def test_rejects_non_list(self) -> None:
        out = ResultValidator.validate({"a": 1})
        self.assertFalse(out.is_valid)

    def test_rejects_empty_list(self) -> None:
        out = ResultValidator.validate([])
        self.assertFalse(out.is_valid)

    def test_rejects_non_dict_row(self) -> None:
        out = ResultValidator.validate([["x"]])
        self.assertFalse(out.is_valid)

    def test_accepts_list_of_dict_rows(self) -> None:
        out = ResultValidator.validate([{"gender": "male", "avg_add": 4.0}])
        self.assertTrue(out.is_valid)


class AnswerQualityValidatorUnitTests(unittest.TestCase):
    def test_rejects_empty_answer(self) -> None:
        out = AnswerQualityValidator.validate("", rows=[{"a": 1}], sql="SELECT 1")
        self.assertFalse(out.is_valid)

    def test_rejects_error_string(self) -> None:
        out = AnswerQualityValidator.validate("Error generating answer: boom", rows=[{"a": 1}], sql="SELECT 1")
        self.assertFalse(out.is_valid)

    def test_rejects_cannot_answer_when_sql_and_rows_present(self) -> None:
        out = AnswerQualityValidator.validate("I cannot answer this.", rows=[{"a": 1}], sql="SELECT 1")
        self.assertFalse(out.is_valid)

    def test_accepts_normal_answer(self) -> None:
        out = AnswerQualityValidator.validate("Average addiction is higher for males.", rows=[{"a": 1}], sql="SELECT 1")
        self.assertTrue(out.is_valid)


if __name__ == "__main__":
    unittest.main()
