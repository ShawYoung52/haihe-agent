#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ECMWF HRES (open data) 历史回放下载脚本

用途：
- 按历史起报时次（00/12）批量下载降水预报常用变量
- 固定下载 step: +12/+24/+36/+48/+60/+72h
- 便于回放验证应急响应流程是否正常

依赖：
    pip install ecmwf-opendata
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ecmwf.opendata import Client

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")


@dataclass(frozen=True)
class DownloadConfig:
    # 历史回放区间（包含两端），按 UTC 日期处理
    start_date: str = "2025-08-20"
    end_date: str = "2025-08-26"
    # 历史起报时次（UTC），常用 00/12
    cycles: tuple[int, ...] = (0, 12)
    # 固定预报时效（小时）
    steps: tuple[int, ...] = (12, 24, 36, 48, 60, 72)
    # 变量：总降水 + 对流环境辅助
    params: tuple[str, ...] = ("tp", "cape", "tcwv", "10u", "10v", "msl")
    # 单层参数（sfc）
    levtype: str = "sfc"
    # 可选区域裁剪 [N, W, S, E]，open-data 通道通常不支持服务端 area；
    # 若有值，仅用于日志提示（建议下载后本地裁剪）。
    area: tuple[float, float, float, float] | None = (42.0, 112.0, 34.0, 121.0)
    # 输出目录
    root_dir: str = r"D:\EC_HRES_HISTORICAL_OPEN"
    # 是否覆盖已存在文件
    overwrite: bool = False
    # 先自动探测最近可用起报时次（只探测 step=12）
    auto_probe_latest: bool = True
    # 最多向前回溯多少天进行探测
    probe_lookback_days: int = 21


CFG = DownloadConfig()


def _iter_dates(start_date: str, end_date: str) -> Iterable[datetime]:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if end_dt < start_dt:
        raise ValueError("end_date 不能早于 start_date")
    curr = start_dt
    while curr <= end_dt:
        yield curr
        curr += timedelta(days=1)


def _build_target_path(root: Path, base_time: datetime, step: int) -> Path:
    yyyymm = base_time.strftime("%Y%m")
    yyyymmddhh = base_time.strftime("%Y%m%d%H")
    target_dir = root / str(base_time.year) / yyyymm / yyyymmddhh
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"ecmwf_hres_sfc_step{step:03d}.grib2"


def _download_one_step(
    client: Client,
    base_time: datetime,
    step: int,
    target: Path,
    cfg: DownloadConfig,
) -> None:
    if target.exists() and target.stat().st_size > 0 and not cfg.overwrite:
        logging.info("[SKIP] 文件已存在: %s", target)
        return

    request = {
        "date": int(base_time.strftime("%Y%m%d")),
        "time": int(base_time.strftime("%H")),
        "stream": "oper",
        "type": "fc",
        "step": step,
        "param": list(cfg.params),
        "levtype": cfg.levtype,
    }

    logging.info(
        "[GET] date=%s time=%s step=%sh -> %s",
        base_time.strftime("%Y-%m-%d"),
        base_time.strftime("%H"),
        step,
        target.name,
    )
    client.retrieve(request, str(target))
    logging.info("[DONE] %s", target)


def _is_missing_error(exc: Exception) -> bool:
    txt = str(exc)
    return ("404" in txt) or ("Not Found" in txt)


def _build_open_data_index_url(base_time: datetime, step: int, stream: str = "oper") -> str:
    day = base_time.strftime("%Y%m%d")
    hh = base_time.strftime("%H")
    stamp = base_time.strftime("%Y%m%d%H0000")
    return (
        f"https://data.ecmwf.int/forecasts/{day}/{hh}z/ifs/0p25/"
        f"{stream}/{stamp}-{step}h-{stream}-fc.index"
    )


def _url_exists(url: str, timeout_sec: int = 15) -> bool:
    req = Request(url, method="HEAD")
    try:
        with urlopen(req, timeout=timeout_sec):
            return True
    except HTTPError as exc:
        if exc.code == 404:
            return False
        return False
    except URLError:
        return False


def _find_latest_available_base(cfg: DownloadConfig) -> datetime | None:
    now_utc = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for back in range(cfg.probe_lookback_days + 1):
        day = (now_utc - timedelta(days=back)).replace(hour=0)
        for cycle in sorted(cfg.cycles, reverse=True):
            base = day.replace(hour=cycle)
            # 与当前时刻相比尚未起报的时次无需探测
            if base > now_utc:
                continue
            url = _build_open_data_index_url(base, step=cfg.steps[0], stream="oper")
            if _url_exists(url):
                return base
    return None


def main() -> None:
    root = Path(CFG.root_dir)
    root.mkdir(parents=True, exist_ok=True)

    # ECMWF open data 客户端；默认为官方开放端点
    client = Client(source="ecmwf")

    total_jobs = 0
    failed_jobs = 0
    skipped_jobs = 0

    if CFG.area is not None:
        logging.info(
            "[NOTE] open-data 通道不使用服务端 area=%s；请在下载后做本地裁剪。",
            CFG.area,
        )

    date_windows = list(_iter_dates(CFG.start_date, CFG.end_date))
    cycles = CFG.cycles
    if CFG.auto_probe_latest:
        latest_base = _find_latest_available_base(CFG)
        if latest_base is None:
            logging.warning(
                "[PROBE] 回溯 %s 天仍未发现可用 open-data 起报时次；将按配置日期继续尝试。",
                CFG.probe_lookback_days,
            )
        else:
            probe_day = latest_base.strftime("%Y-%m-%d")
            logging.info(
                "[PROBE] 探测到最近可用起报：%s（将下载该日 %sUTC）",
                latest_base.strftime("%Y-%m-%d %HUTC"),
                latest_base.strftime("%H"),
            )
            date_windows = [datetime.strptime(probe_day, "%Y-%m-%d")]
            cycles = (latest_base.hour,)

    for day in date_windows:
        for cycle in cycles:
            base_time = day.replace(hour=cycle, minute=0, second=0, microsecond=0)
            cycle_available = True
            for step in CFG.steps:
                total_jobs += 1
                target = _build_target_path(root, base_time, step)
                if not cycle_available:
                    skipped_jobs += 1
                    logging.info(
                        "[SKIP] %s %sUTC step=%s（该起报时次在 open-data 不可用）",
                        base_time.strftime("%Y-%m-%d"),
                        base_time.strftime("%H"),
                        step,
                    )
                    continue
                try:
                    _download_one_step(client, base_time, step, target, CFG)
                except Exception as exc:
                    if _is_missing_error(exc):
                        cycle_available = False
                        failed_jobs += 1
                        logging.warning(
                            "[MISS] %s step=%s 不存在（%s），后续 step 将整组跳过",
                            base_time.strftime("%Y-%m-%d %HUTC"),
                            step,
                            str(exc),
                        )
                    else:
                        failed_jobs += 1
                        logging.error(
                            "[ERR] %s step=%s 下载失败: %s",
                            base_time.strftime("%Y-%m-%d %HUTC"),
                            step,
                            exc,
                        )
                    if target.exists() and target.stat().st_size == 0:
                        target.unlink(missing_ok=True)

    logging.info(
        "[FINISH] 总任务=%s, 失败=%s, 跳过=%s, 成功=%s",
        total_jobs,
        failed_jobs,
        skipped_jobs,
        total_jobs - failed_jobs - skipped_jobs,
    )


if __name__ == "__main__":
    main()
