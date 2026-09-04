"""EC AIFS 历史数据清理（保留最近 N 天，默认 14）。

背景（2026-09-04）：服务器定时从 EC 拉取 AIFS 预报数据落本地盘，磁盘已满。
存储根迁移到 NAS（消费侧经 EC_AIFS_ROOT / EC_OUTPUT_PATH 指过去）后，历史数据
按保留窗口滚动清理——消费侧所有代码只按「目标日」读最近起报时次，从不往回翻
历史 EC 文件，窗口外的日目录/文件对业务是死数据，仅保留供排查/重跑。

清理对象（两种已知布局，其它一律不动）：
1. 按日目录：{root}/{YYYY}/{YYYYMMDD}/  —— 整目录删除（日期 < 截止日）。
2. 扁平 output：{root}/output/ 下按文件名日期前缀的过期货：
   - ec_YYYYMMDDHH_rain_total_{N}h.tif
   - {YYYYMMDDHHMMSS}-{N}h-oper-fc.grib2

安全口径：
- 只认精确命名模式 + 合法日期；不匹配的一律跳过，绝不删未知文件/目录。
- 日目录与年份目录交叉校验（YYYYMMDD 年份须等于父目录名），防误伤。
- 不跟随符号链接（软链目录跳过不删）。
- --dry-run 只列出不删除；删除失败逐条记录、最后以退出码 1 汇总（cron 可见）。

用法：
    python scripts/cleanup_ec_aifs.py --dry-run
    python scripts/cleanup_ec_aifs.py --days 14
环境变量：EC_AIFS_ROOT（根目录）、EC_AIFS_RETENTION_DAYS（保留天数）。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("cleanup_ec_aifs")

DEFAULT_RETENTION_DAYS = 14
DEFAULT_EC_AIFS_ROOT = "/home/ev/data/ec/EC_AIFS"
RETENTION_DAYS_ENV = "EC_AIFS_RETENTION_DAYS"

_YEAR_DIR_RE = re.compile(r"^\d{4}$")
_DAY_DIR_RE = re.compile(r"^\d{8}$")
_TIF_NAME_RE = re.compile(r"^ec_(\d{10})_rain_total_\d+h\.tif$", re.IGNORECASE)
_GRIB_NAME_RE = re.compile(r"^(\d{14})-\d+h-oper-fc\.grib2$", re.IGNORECASE)


def parse_retention_days(value: object, default: int = DEFAULT_RETENTION_DAYS) -> int:
    """保留天数安全解析：非法/越界回退默认（导入与运行期都不抛错）。"""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= 366 else default


def _valid_ymd(text: str) -> date | None:
    """8 位数字转合法日期，非法返回 None。"""
    if not _DAY_DIR_RE.match(text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _file_date_from_name(name: str) -> date | None:
    """从已知 EC 文件名取起报日期；不认识的名字返回 None（不删）。"""
    m = _TIF_NAME_RE.match(name) or _GRIB_NAME_RE.match(name)
    if not m:
        return None
    return _valid_ymd(m.group(1)[:8])


def iter_expired_daily_dirs(root: Path, cutoff: date):
    """产出 {root}/{YYYY}/{YYYYMMDD}/ 中日期早于 cutoff 的日目录（Path）。"""
    if not root.is_dir():
        return
    for year_entry in sorted(root.iterdir()):
        if year_entry.is_symlink() or not year_entry.is_dir():
            continue
        if not _YEAR_DIR_RE.match(year_entry.name):
            continue
        for day_entry in sorted(year_entry.iterdir()):
            if day_entry.is_symlink() or not day_entry.is_dir():
                continue
            day = _valid_ymd(day_entry.name)
            if day is None or day >= cutoff:
                continue
            if day_entry.name[:4] != year_entry.name:
                logger.warning("跳过年份不一致的日目录: %s", day_entry)
                continue
            yield day_entry


def iter_expired_flat_files(output_dir: Path, cutoff: date):
    """产出 output 扁平目录下日期早于 cutoff 的已知 EC 文件（Path）。"""
    if not output_dir.is_dir():
        return
    for entry in sorted(output_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        file_date = _file_date_from_name(entry.name)
        if file_date is None or file_date >= cutoff:
            continue
        yield entry


def cleanup(root: Path, days: int, *, dry_run: bool = False, today: date | None = None) -> dict:
    """执行清理，返回统计 dict；删除失败不中断，计入 errors。"""
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    stats = {"root": str(root), "cutoff": cutoff.isoformat(), "days": days,
             "dry_run": dry_run, "removed_dirs": 0, "removed_files": 0, "errors": 0}

    targets = [(p, True) for p in iter_expired_daily_dirs(root, cutoff)]
    targets += [(p, False) for p in iter_expired_flat_files(root / "output", cutoff)]
    for path, is_dir in targets:
        kind = "目录" if is_dir else "文件"
        if dry_run:
            print(f"[dry-run] 将删除{kind}: {path}")
        else:
            try:
                shutil.rmtree(path) if is_dir else path.unlink()
                logger.info("已删除%s: %s", kind, path)
            except OSError as exc:
                stats["errors"] += 1
                logger.error("删除%s失败: %s (%s)", kind, path, type(exc).__name__)
                continue
        stats["removed_dirs" if is_dir else "removed_files"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EC AIFS 历史数据清理（保留最近 N 天）")
    parser.add_argument("--root", default=os.environ.get("EC_AIFS_ROOT", DEFAULT_EC_AIFS_ROOT),
                        help="EC AIFS 根目录（默认取 EC_AIFS_ROOT 环境变量）")
    parser.add_argument("--days", type=int, default=None,
                        help=f"保留天数（默认取 {RETENTION_DAYS_ENV}，缺省 {DEFAULT_RETENTION_DAYS}）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将删除的内容，不实际删除")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    days = parse_retention_days(args.days if args.days is not None
                                else os.environ.get(RETENTION_DAYS_ENV, ""))
    stats = cleanup(Path(args.root), days, dry_run=args.dry_run)
    print(f"完成: cutoff={stats['cutoff']} 删除目录 {stats['removed_dirs']} 个、"
          f"文件 {stats['removed_files']} 个、失败 {stats['errors']} 个"
          f"{'（dry-run 未实际删除）' if args.dry_run else ''}")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
