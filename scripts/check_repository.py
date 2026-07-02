#!/usr/bin/env python3
"""Repository health checks for local development and CI.

This script intentionally keeps checks lightweight so it can run without
installing the full business runtime dependencies.
"""

from __future__ import annotations

import compileall
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATH_PARTS = {
    ".venv",
    ".venv_new",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "node_modules",
}

FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
}

ALLOWED_ENV_EXAMPLES = {".env.example"}


def run_git_ls_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git ls-files failed")
    return [ROOT / line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_forbidden_tracked_files(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for file_path in files:
        rel = file_path.relative_to(ROOT)
        parts = set(rel.parts)
        if parts & FORBIDDEN_PATH_PARTS:
            errors.append(f"forbidden tracked path: {rel}")
            continue
        if file_path.suffix in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden tracked file suffix: {rel}")
            continue
        if file_path.name.startswith(".env") and file_path.name not in ALLOWED_ENV_EXAMPLES:
            errors.append(f"local env file must not be tracked: {rel}")
    return errors


def check_python_syntax() -> bool:
    targets = [
        ROOT / "haiheliuyubaoyuagent-master" / "chainlitexam",
        ROOT / "haiheliuyubaoyuagent-master" / "haihe-weather-analyzer-mcp",
        ROOT / "scripts",
    ]
    ok = True
    for target in targets:
        if not target.exists():
            continue
        ok = compileall.compile_dir(
            str(target),
            quiet=1,
            force=True,
            rx=os.sep + r"(\.venv|\.venv_new|venv|env|__pycache__)" + os.sep,
        ) and ok
    return ok


def main() -> int:
    errors = check_forbidden_tracked_files(run_git_ls_files())
    if not check_python_syntax():
        errors.append("python syntax check failed")

    if errors:
        print("Repository health check failed:")
        for item in errors:
            print(f"- {item}")
        return 1

    print("Repository health check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
