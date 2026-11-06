"""The Oracle VM's SQLite is 3.34.1. SQL that newer SQLite accepts is a syntax error there, so a feature fails only in
production (``ON CONFLICT DO UPDATE`` without a conflict target broke the self-model this way). Every SQL string in
the bot is scanned for syntax newer than 3.34."""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SKIP_PARTS = {"tests", "venv", ".venv", "site-packages", "node_modules", "__pycache__"}
_SQL_START = re.compile(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|WITH)\b", re.IGNORECASE)
NEWER_THAN_3_34 = {
    "ON CONFLICT DO UPDATE needs a conflict target before 3.35": r"ON\s+CONFLICT\s+DO\s+UPDATE",
    "RETURNING is 3.35+": r"\bRETURNING\b",
    "ALTER TABLE ... DROP COLUMN is 3.35+": r"\bDROP\s+COLUMN\b",
    "MATERIALIZED CTE hints are 3.35+": r"\bMATERIALIZED\b",
    "math functions are 3.35+ (and optional)": r"\b(?:ceil|ceiling|floor|sqrt|pow|power|ln|log2|log10|exp|pi|trunc)\s*\(",
    "STRICT tables are 3.37+": r"\)\s*STRICT\b",
    "-> and ->> are 3.38+": r"->",
    "unixepoch() is 3.38+": r"\bunixepoch\s*\(",
    "RIGHT and FULL joins are 3.39+": r"\b(?:RIGHT|FULL)\s+(?:OUTER\s+)?JOIN\b",
    "IS [NOT] DISTINCT FROM is 3.39+": r"\bIS\s+(?:NOT\s+)?DISTINCT\s+FROM\b",
    "unhex/octet_length/timediff/concat/string_agg/format are 3.41+": (
        r"\b(?:unhex|octet_length|timediff|concat|concat_ws|string_agg|format)\s*\("
    ),
}


def _sql_strings():
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if any(part in _SKIP_PARTS or part.startswith(".") or "blender" in part for part in parts):
            continue
        if parts[-1].startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL_START.match(node.value):
                yield f"{path.relative_to(ROOT)}:{node.lineno}", node.value


def problems_in(sql):
    found = [reason for reason, pattern in NEWER_THAN_3_34.items() if re.search(pattern, sql, re.IGNORECASE)]
    if sql.upper().count("ON CONFLICT") > 1:
        found.append("several ON CONFLICT clauses are 3.35+")
    return found


class SQLite334CompatibilityTest(unittest.TestCase):
    def test_no_sql_newer_than_sqlite_3_34(self):
        problems = [f"{where}: {reason}" for where, sql in _sql_strings() for reason in problems_in(sql)]
        self.assertEqual(problems, [], "the Oracle VM runs SQLite 3.34.1")

    def test_the_scan_sees_the_constructs(self):
        self.assertTrue(problems_in("INSERT INTO t(a) VALUES(1) ON CONFLICT DO UPDATE SET a=2"))
        self.assertFalse(problems_in("INSERT INTO t(a) VALUES(1) ON CONFLICT(a) DO UPDATE SET a=2"))
        self.assertTrue(problems_in("DELETE FROM t WHERE a=1 RETURNING a"))
        self.assertTrue(problems_in("SELECT payload->>'$.x' FROM t"))
        self.assertTrue(problems_in("INSERT INTO t VALUES(1) ON CONFLICT(a) DO NOTHING ON CONFLICT(b) DO NOTHING"))
        self.assertFalse(problems_in("SELECT json_extract(payload, '$.x'), MIN(a), MAX(b) FROM t"))


if __name__ == "__main__":
    unittest.main()
