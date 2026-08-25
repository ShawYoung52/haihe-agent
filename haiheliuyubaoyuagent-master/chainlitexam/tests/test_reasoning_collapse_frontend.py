"""Chainlit 2.9.6 思考过程自动折叠前端兼容契约。"""

from pathlib import Path
import shutil
import subprocess

import pytest


CHAINLIT_DIR = Path(__file__).resolve().parents[1]
CUSTOM_JS = CHAINLIT_DIR / "public" / "img-zoom.js"
AGENTWEB_JS = CHAINLIT_DIR / "AgentWeb" / "img-zoom-agentweb.js"
BEHAVIOR_TEST = Path(__file__).with_name("reasoning_auto_collapse.test.js")
CONFIG = CHAINLIT_DIR / ".chainlit" / "config.toml"


def test_config_loads_reasoning_collapse_compatible_script():
    config = CONFIG.read_text(encoding="utf-8")
    assert 'custom_js = "/public/img-zoom.js"' in config


def test_custom_js_waits_for_answer_then_collapses_reasoning_step():
    script = CUSTOM_JS.read_text(encoding="utf-8")
    assert "chainlit_reasoning_complete" in script
    assert "MutationObserver" in script
    assert '[data-step-type="assistant_message"]' in script
    assert 'button[aria-expanded="true"]' in script
    assert ".loading-cursor" in script
    assert 'getElementById("stop-button")' in script
    assert 'window.addEventListener("message"' in script
    assert '"message" in payload' in script


def test_agentweb_script_waits_for_stream_completion_before_collapsing():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        [node, str(BEHAVIOR_TEST), str(AGENTWEB_JS)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reasoning auto-collapse behavior: ok" in result.stdout
