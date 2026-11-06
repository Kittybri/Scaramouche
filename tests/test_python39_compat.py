"""The Oracle server runs the bots on Python 3.9. Two things that are fine on 3.11 crash the bot at start-up there:

* an ``X | None`` annotation in a module without ``from __future__ import annotations`` (Python 3.9 evaluates
  function annotations when the function is defined, and ``str | None`` is a TypeError before 3.10);
* a possessive quantifier (``@++``) or an atomic group (``(?>...)``) in a regular expression (new in 3.11).

Both shipped once and were caught only by importing the code on 3.9, so they are checked here on any version.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SKIP_PARTS = {"tests", "venv", ".venv", "site-packages", "node_modules", "__pycache__"}
_RE_FUNCS = {"compile", "sub", "subn", "search", "match", "fullmatch", "findall", "finditer", "split"}


def _bot_modules():
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if any(part in _SKIP_PARTS or part.startswith(".") or "blender" in part for part in parts):
            continue  # tests, environments, and Blender scripts (those run inside Blender's own Python)
        if parts[-1].startswith("test_"):
            continue
        yield path


def _has_future_annotations(tree):
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _evaluated_annotations(tree):
    """Annotations Python 3.9 evaluates: every function signature, and variable annotations at module/class level."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
    for scope in [tree, *(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))]:
        for statement in scope.body:
            if isinstance(statement, ast.AnnAssign):
                yield statement.annotation


def _uses_union_operator(annotation):
    return any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) for node in ast.walk(annotation))


def _literal_patterns(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _RE_FUNCS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "re" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            yield node.args[0].lineno, node.args[0].value


def _needs_python_311(pattern):
    """True when the pattern uses a possessive quantifier or an atomic group (Python 3.11+ only)."""
    try:
        from re import _constants as constants, _parser as parser  # Python 3.11+
    except ImportError:
        return False  # on older Pythons such a pattern does not even compile, so importing the module fails instead
    newer = {constants.POSSESSIVE_REPEAT, constants.ATOMIC_GROUP}

    def walk(item):
        if isinstance(item, parser.SubPattern):
            return any(op in newer or walk(arg) for op, arg in item.data)
        if isinstance(item, (list, tuple)):
            return any(walk(part) for part in item)
        return False

    try:
        return walk(parser.parse(pattern))
    except re.error:
        return False


class Python39CompatibilityTest(unittest.TestCase):
    def test_union_annotations_only_with_future_import(self):
        problems = []
        for path in _bot_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            if _has_future_annotations(tree):
                continue
            for annotation in _evaluated_annotations(tree):
                if _uses_union_operator(annotation):
                    problems.append(f"{path.relative_to(ROOT)}:{annotation.lineno}")
        self.assertEqual(problems, [], "add 'from __future__ import annotations' (Python 3.9 runs these modules)")

    @unittest.skipIf(sys.version_info < (3, 11), "the regex parser that can see these constructs is 3.11+")
    def test_no_regex_features_newer_than_python_39(self):
        problems = []
        for path in _bot_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for lineno, pattern in _literal_patterns(tree):
                if _needs_python_311(pattern):
                    problems.append(f"{path.relative_to(ROOT)}:{lineno}")
        import narration  # its patterns are assembled from pieces, so check the compiled ones too

        for name, value in vars(narration).items():
            if isinstance(value, re.Pattern) and _needs_python_311(value.pattern):
                problems.append(f"narration.{name}")
        self.assertEqual(problems, [], "possessive quantifiers / atomic groups need Python 3.11")

    @unittest.skipIf(sys.version_info < (3, 11), "the regex parser that can see these constructs is 3.11+")
    def test_the_check_sees_possessive_quantifiers(self):
        self.assertTrue(_needs_python_311(r"(?<!@)@++(?=everyone|here)"))
        self.assertTrue(_needs_python_311(r"(?>ab|a)c"))
        self.assertFalse(_needs_python_311(r"(?<!@)@+(?=everyone|here)"))
        self.assertFalse(_needs_python_311(r"\++|[+*]"))

    def test_the_check_sees_union_annotations(self):
        tree = ast.parse("def f(x: str | None = None) -> int: ...\nclass A:\n    y: int | None = None\n")
        self.assertEqual(sum(_uses_union_operator(a) for a in _evaluated_annotations(tree)), 2)
        local = ast.parse("def g():\n    z: int | None = None\n")
        self.assertFalse(any(_uses_union_operator(a) for a in _evaluated_annotations(local)))


if __name__ == "__main__":
    unittest.main()
