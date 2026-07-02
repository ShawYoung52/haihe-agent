from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "haiheliuyubaoyuagent-master" / "chainlitexam" / "settings.py"


def load_settings_module():
    module_name = "chainlit_gateway_settings_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, SETTINGS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ChainlitSettingsTests(unittest.TestCase):
    def setUp(self):
        self._old_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_defaults_use_local_addresses(self):
        for key in [
            "CHAINLIT_DB_HOST",
            "DB_HOST",
            "MCP_WEATHER_URL",
            "MCP_SERVER_URL",
            "OPENAI_API_BASE",
        ]:
            os.environ.pop(key, None)

        settings_module = load_settings_module()
        settings = settings_module.get_settings()

        self.assertEqual(settings.chainlit_db_host, "127.0.0.1")
        self.assertEqual(settings.river_plot_db_host, "127.0.0.1")
        self.assertEqual(settings.mcp_weather_url, "http://127.0.0.1:3333/sse")
        self.assertEqual(settings.openai_api_base, "http://127.0.0.1:8000/v1/")

    def test_apply_env_defaults_populates_legacy_names(self):
        os.environ.clear()
        settings_module = load_settings_module()
        settings_module.apply_env_defaults()

        self.assertEqual(os.environ["MCP_SERVER_URL"], "http://127.0.0.1:3333/sse")
        self.assertEqual(os.environ["CHAINLIT_DB_HOST"], "127.0.0.1")
        self.assertEqual(os.environ["OPENAI_MODEL"], "Qwen3.6-27B")

    def test_production_validation_rejects_weak_values(self):
        os.environ.clear()
        os.environ["APP_ENV"] = "production"
        settings_module = load_settings_module()

        with self.assertRaises(RuntimeError):
            settings_module.get_settings()


if __name__ == "__main__":
    unittest.main()
