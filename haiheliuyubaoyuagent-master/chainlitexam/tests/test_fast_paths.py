"""Static check for fast paths in message_orchestrator.py.

This script parses ``chainlitexam/message_orchestrator.py`` and verifies that
every ``_try_*_fast_path`` async function:

1. Creates a business reasoning step (contains ``_show_business_reasoning``).
2. Closes the reasoning step in all paths (contains
   ``await reasoning.close()`` or a ``finally`` block).

Run directly with ``python tests/test_fast_paths.py``.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "message_orchestrator.py"


def _find_fast_path_functions(source: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name.startswith("_try_") and node.name.endswith("_fast_path"):
                yield node


def _function_source(func: ast.AsyncFunctionDef, source: str) -> str:
    lines = source.splitlines()
    return "\n".join(lines[func.lineno - 1 : func.end_lineno])


def _has_finally_block(func: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Try) and node.finalbody:
            return True
    return False


def check_fast_paths() -> bool:
    source = SRC.read_text(encoding="utf-8")
    all_ok = True
    results = []

    for func in sorted(_find_fast_path_functions(source), key=lambda f: f.lineno):
        body = _function_source(func, source)
        has_reasoning = "_show_business_reasoning" in body
        has_close = "await reasoning.close()" in body
        has_finally = _has_finally_block(func)
        ok = has_reasoning and (has_close or has_finally)
        if not ok:
            all_ok = False
        results.append(
            {
                "name": func.name,
                "ok": ok,
                "has_reasoning": has_reasoning,
                "has_close": has_close,
                "has_finally": has_finally,
            }
        )
        status = "PASS" if ok else "FAIL"
        print(
            f"[{status}] {func.name}: "
            f"reasoning={has_reasoning}, close={has_close}, finally={has_finally}"
        )

    passed = sum(r["ok"] for r in results)
    print(f"\nTotal: {len(results)} fast paths, {passed} passed.")
    return all_ok


if __name__ == "__main__":
    ok = check_fast_paths()
    sys.exit(0 if ok else 1)
