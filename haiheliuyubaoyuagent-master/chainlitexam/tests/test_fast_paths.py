"""Static check for fast paths in message_orchestrator.py.

This script parses ``chainlitexam/message_orchestrator.py`` and verifies that
every ``_try_*_fast_path`` async function:

1. Creates a business reasoning step by calling ``_show_business_reasoning(...)``.
2. Closes the reasoning step on every control path where it is active, either by
   placing ``reasoning.close()`` in a ``finally`` block or by calling it
   unconditionally before each return.

Run directly with ``python tests/test_fast_paths.py``.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "message_orchestrator.py"


def _find_fast_path_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name.startswith("_try_") and node.name.endswith("_fast_path"):
                yield node


def _is_show_business_reasoning_call(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_show_business_reasoning":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "_show_business_reasoning":
            return True
    return False


def _is_reasoning_close_call(node: ast.AST) -> bool:
    """Match ``reasoning.close()`` or ``await reasoning.close()``."""
    if isinstance(node, ast.Await):
        return _is_reasoning_close_call(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "close":
            value = func.value
            if isinstance(value, ast.Name) and value.id == "reasoning":
                return True
    return False


def _statement_has_close(stmt: ast.stmt) -> bool:
    return any(_is_reasoning_close_call(n) for n in ast.walk(stmt))


def _is_reasoning_assignment(stmt: ast.stmt) -> bool:
    """Return True if ``stmt`` assigns ``reasoning = ...`` from the helper."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == "reasoning":
                if stmt.value and _is_show_business_reasoning_call(stmt.value):
                    return True
                if isinstance(stmt.value, ast.Await) and _is_show_business_reasoning_call(
                    stmt.value.value
                ):
                    return True
    elif isinstance(stmt, ast.AnnAssign):
        if isinstance(stmt.target, ast.Name) and stmt.target.id == "reasoning":
            if stmt.value and _is_show_business_reasoning_call(stmt.value):
                return True
            if isinstance(stmt.value, ast.Await) and _is_show_business_reasoning_call(
                stmt.value.value
            ):
                return True
    return False


def _block_covers_returns(body: list[ast.stmt], reasoning_active: bool = False) -> bool:
    """
    Recursively check that no ``return`` is reached while ``reasoning`` is
    active without an intervening ``reasoning.close()``.

    ``reasoning_active`` tracks whether a ``ReasoningStep`` has been created on
    the current path and has not yet been closed. A ``finally`` block that
    closes reasoning resets the flag for all code after it.
    """
    for stmt in body:
        if isinstance(stmt, ast.Return):
            if reasoning_active:
                return False
        elif _is_reasoning_assignment(stmt):
            reasoning_active = True
        elif _statement_has_close(stmt):
            # ``reasoning.close()`` is idempotent; after this point the object
            # is closed, so subsequent returns do not need another close.
            reasoning_active = False
        elif isinstance(stmt, ast.If):
            if not _block_covers_returns(stmt.body, reasoning_active):
                return False
            if not _block_covers_returns(stmt.orelse, reasoning_active):
                return False
        elif isinstance(stmt, ast.Try):
            finally_closes = bool(
                stmt.finalbody and any(_statement_has_close(s) for s in stmt.finalbody)
            )
            if finally_closes:
                # Inside the try, any active reasoning will be closed in finally.
                if not _block_covers_returns(stmt.body, reasoning_active=False):
                    return False
                for handler in stmt.handlers:
                    if not _block_covers_returns(handler.body, reasoning_active=False):
                        return False
                if not _block_covers_returns(stmt.finalbody, reasoning_active=False):
                    return False
                # After a finally that closes reasoning, the object is closed.
                reasoning_active = False
            else:
                if not _block_covers_returns(stmt.body, reasoning_active):
                    return False
                for handler in stmt.handlers:
                    if not _block_covers_returns(handler.body, reasoning_active):
                        return False
                if not _block_covers_returns(stmt.finalbody, reasoning_active):
                    return False
        elif isinstance(stmt, (ast.For, ast.While, ast.AsyncFor)):
            if not _block_covers_returns(stmt.body, reasoning_active):
                return False
            if not _block_covers_returns(stmt.orelse, reasoning_active):
                return False
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            if not _block_covers_returns(stmt.body, reasoning_active):
                return False
        elif isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            # Do not descend into nested scopes.
            continue
    return True


def check_fast_paths() -> bool:
    source = SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)

    all_ok = True
    results = []

    for func in sorted(_find_fast_path_functions(tree), key=lambda f: f.lineno):
        has_reasoning_call = any(
            _is_show_business_reasoning_call(node) for node in ast.walk(func)
        )
        returns_covered = _block_covers_returns(func.body)

        ok = has_reasoning_call and returns_covered
        if not ok:
            all_ok = False

        results.append(
            {
                "name": func.name,
                "ok": ok,
                "has_reasoning_call": has_reasoning_call,
                "returns_covered": returns_covered,
            }
        )
        status = "PASS" if ok else "FAIL"
        print(
            f"[{status}] {func.name}: "
            f"reasoning_call={has_reasoning_call}, "
            f"returns_covered={returns_covered}"
        )

    passed = sum(r["ok"] for r in results)
    print(f"\nTotal: {len(results)} fast paths, {passed} passed.")
    return all_ok


if __name__ == "__main__":
    ok = check_fast_paths()
    sys.exit(0 if ok else 1)
