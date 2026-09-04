"""EC AIFS 历史数据清理脚本测试（2026-09-04）。

口径：保留最近 N 天（默认 14），只删两种已知布局（按日目录 / output 扁平命名），
不匹配的一律不动；不跟随符号链接；--dry-run 不删除。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "cleanup_ec_aifs", MCP_DIR / "scripts" / "cleanup_ec_aifs.py"
)
cla = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cla)

TODAY = date(2026, 9, 4)


def _mk_day_dir(root: Path, d: date, files=("a.grib2",)) -> Path:
    day_dir = root / f"{d.year:04d}" / d.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        (day_dir / f).write_text("x", encoding="utf-8")
    return day_dir


# ---------------------------------------------------------------------------
# parse_retention_days
# ---------------------------------------------------------------------------

def test_parse_retention_days_valid():
    assert cla.parse_retention_days("7") == 7
    assert cla.parse_retention_days(30) == 30
    assert cla.parse_retention_days(" 14 ") == 14


@pytest.mark.parametrize("bad", ["", "abc", None, "0", "-3", "999", "1.5x"])
def test_parse_retention_days_invalid_falls_back(bad):
    assert cla.parse_retention_days(bad) == cla.DEFAULT_RETENTION_DAYS


# ---------------------------------------------------------------------------
# 日期解析
# ---------------------------------------------------------------------------

def test_valid_ymd():
    assert cla._valid_ymd("20260904") == date(2026, 9, 4)
    assert cla._valid_ymd("20261301") is None  # 13 月非法
    assert cla._valid_ymd("2026090") is None
    assert cla._valid_ymd("abcdefgh") is None


def test_file_date_from_name_known_patterns():
    assert cla._file_date_from_name("ec_2026090408_rain_total_24h.tif") == date(2026, 9, 4)
    assert cla._file_date_from_name("20260904080000-24h-oper-fc.grib2") == date(2026, 9, 4)


@pytest.mark.parametrize("name", ["readme.txt", "ec_2026090408_tem_2m.tif",
                                  "20260904080000-24h-oper-fc.grib2.bak", "ec_xx_rain_total_24h.tif"])
def test_file_date_from_name_unknown_returns_none(name):
    assert cla._file_date_from_name(name) is None


# ---------------------------------------------------------------------------
# 按日目录清理
# ---------------------------------------------------------------------------

def test_cleanup_removes_only_expired_daily_dirs(tmp_path):
    root = tmp_path / "EC_AIFS"
    old = _mk_day_dir(root, TODAY - timedelta(days=20))
    boundary = _mk_day_dir(root, TODAY - timedelta(days=13))  # 窗口内最新一天
    today_dir = _mk_day_dir(root, TODAY)
    stats = cla.cleanup(root, 14, today=TODAY)
    assert not old.exists()
    assert boundary.exists() and today_dir.exists()
    assert stats["removed_dirs"] == 1 and stats["errors"] == 0


def test_cleanup_dry_run_deletes_nothing(tmp_path):
    root = tmp_path / "EC_AIFS"
    old = _mk_day_dir(root, TODAY - timedelta(days=30))
    stats = cla.cleanup(root, 14, dry_run=True, today=TODAY)
    assert old.exists()
    assert stats["removed_dirs"] == 1 and stats["dry_run"] is True


def test_cleanup_skips_non_pattern_entries(tmp_path):
    root = tmp_path / "EC_AIFS"
    stray_year = root / "2026" / "not-a-date"
    stray_year.mkdir(parents=True)
    other_dir = root / "2026" / "backup"
    other_dir.mkdir()
    (root / "keep.txt").write_text("x", encoding="utf-8")
    weird_year = root / "20A6" / "20200101"
    weird_year.mkdir(parents=True)
    stats = cla.cleanup(root, 14, today=TODAY)
    assert stray_year.exists() and other_dir.exists() and weird_year.exists()
    assert (root / "keep.txt").exists()
    assert stats["removed_dirs"] == 0


def test_cleanup_skips_year_mismatch_day_dir(tmp_path):
    root = tmp_path / "EC_AIFS"
    mismatch = root / "2026" / "20200101"  # 日目录年份与父目录不一致
    mismatch.mkdir(parents=True)
    stats = cla.cleanup(root, 14, today=TODAY)
    assert mismatch.exists()
    assert stats["removed_dirs"] == 0


def test_cleanup_skips_symlink_day_dir(tmp_path):
    root = tmp_path / "EC_AIFS"
    real = _mk_day_dir(root, TODAY - timedelta(days=20))
    link = root / "2026" / (TODAY - timedelta(days=21)).strftime("%Y%m%d")
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("平台不支持符号链接")
    cla.cleanup(root, 14, today=TODAY)
    assert link.exists() or link.is_symlink()


def test_cleanup_missing_root_ok(tmp_path):
    stats = cla.cleanup(tmp_path / "nope", 14, today=TODAY)
    assert stats["removed_dirs"] == 0 and stats["errors"] == 0


# ---------------------------------------------------------------------------
# output 扁平目录
# ---------------------------------------------------------------------------

def test_cleanup_flat_output_files(tmp_path):
    root = tmp_path / "EC_AIFS"
    out = root / "output"
    out.mkdir(parents=True)
    old_d = (TODAY - timedelta(days=30)).strftime("%Y%m%d")
    new_d = TODAY.strftime("%Y%m%d")
    old_tif = out / f"ec_{old_d}08_rain_total_24h.tif"
    old_grib = out / f"{old_d}080000-24h-oper-fc.grib2"
    new_tif = out / f"ec_{new_d}08_rain_total_24h.tif"
    unknown = out / "ec_2020010108_tem_2m.tif"  # 未知要素名不动
    for f in (old_tif, old_grib, new_tif, unknown):
        f.write_text("x", encoding="utf-8")
    stats = cla.cleanup(root, 14, today=TODAY)
    assert not old_tif.exists() and not old_grib.exists()
    assert new_tif.exists() and unknown.exists()
    assert stats["removed_files"] == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_main_dry_run_exit_0(tmp_path, capsys):
    root = tmp_path / "EC_AIFS"
    _mk_day_dir(root, date.today() - timedelta(days=40))
    rc = cla.main(["--root", str(root), "--days", "14", "--dry-run"])
    assert rc == 0
    assert "dry-run" in capsys.readouterr().out


def test_main_env_retention_days(tmp_path, monkeypatch):
    root = tmp_path / "EC_AIFS"
    d15 = _mk_day_dir(root, date.today() - timedelta(days=15))
    monkeypatch.setenv(cla.RETENTION_DAYS_ENV, "20")
    rc = cla.main(["--root", str(root)])
    assert rc == 0
    assert d15.exists(), "env 20 天窗口下 15 天前的目录应保留"
