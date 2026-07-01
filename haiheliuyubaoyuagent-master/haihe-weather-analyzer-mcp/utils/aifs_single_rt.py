# -*- coding: utf-8 -*-
"""
Created on Tue Jan 27 17:03:03 2026

@author: Administrator
"""
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
import importlib
import os
from pathlib import Path

# 终端少刷屏：CDS 排队阶段 urllib3 DEBUG 会淹没有用信息
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)

# CDS：若 .cdsapirc 里 url 写成 .../api/v2，会 404（v2 已下线）。
# 官方现为 url: https://cds.climate.copernicus.eu/api ，key 为个人访问令牌（不含冒号，走新客户端）。

# 历史下载配置（ERA5 单层：仅总降水 total_precipitation）
CFG = {
    "times": [0, 6, 12, 18],
    "params": ["total_precipitation"],
    "dataset": "reanalysis-era5-single-levels",
    "format": "grib",
    "root_dir": r"D:\EC_AIFS_HISTORICAL"  # 根目录：年/年月/ 下为按月合并的 grib
}

# 初始化 CDS 客户端（历史数据）
try:
    cdsapi_mod = importlib.import_module("cdsapi")
    get_url_key_verify = importlib.import_module("cdsapi.api").get_url_key_verify
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "缺少依赖 `cdsapi`。请先安装：pip install cdsapi"
    ) from exc


def _normalize_cds_url(url: str | None) -> str | None:
    if not url:
        return url
    u = url.rstrip("/")
    if u.endswith("/v2"):
        u = u[:-3]
    return u


def _make_cds_client():
    url, key, verify = get_url_key_verify(None, None, None)
    url = _normalize_cds_url(url)
    # 无冒号 = 个人访问令牌（PAT），走 ecmwf-datastores / 新接口
    if key and ":" not in key:
        LegacyClient = importlib.import_module(
            "ecmwf.datastores.legacy_client"
        ).LegacyClient
        return LegacyClient(url=url, key=key, verify=verify, quiet=True)
    return cdsapi_mod.Client(url=url, key=key, verify=verify, quiet=True)


client = _make_cds_client()


def _expand_interval(start_str: str, end_str: str) -> list[datetime]:
    start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    n = (end_dt - start_dt).days
    return [start_dt + timedelta(days=i) for i in range(n + 1)]


def _group_by_year_month(dates: list[datetime]) -> list[tuple[int, int, list[str]]]:
    """同一自然月内的多个日历日合并为一次 CDS 请求（day 为列表）。"""
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for dt in dates:
        buckets[(dt.year, dt.month)].append(dt.day)
    out: list[tuple[int, int, list[str]]] = []
    for (year, month) in sorted(buckets.keys()):
        days_unique = sorted(set(buckets[(year, month)]))
        day_strs = [f"{d:02d}" for d in days_unique]
        out.append((year, month, day_strs))
    return out


def build_era5_request_batch(year: int, month: int, days: list[str]) -> dict:
    return {
        "product_type": "reanalysis",
        "variable": CFG["params"],
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": days,
        "time": [f"{t:02d}:00" for t in CFG["times"]],
        "data_format": CFG["format"],
        "download_format": "unarchived",
    }


def _cds_retrieve_with_heartbeat(
    dataset: str, request: dict, target: str, interval_sec: int = 60
) -> None:
    """CDS 在 Accepted/排队阶段不会开始传文件；阻塞期间周期性打印，避免误以为卡死。"""
    stop = threading.Event()

    def _loop() -> None:
        elapsed_sec = 0
        while True:
            if stop.wait(interval_sec):
                return
            elapsed_sec += interval_sec
            print(
                "  [CDS] 仍在等待（网页常为 Accepted / Queued）："
                "服务端未出结果前不会下载，已约 "
                f"{elapsed_sec // 60} 分钟…",
                flush=True,
            )

    th = threading.Thread(target=_loop, daemon=True)
    th.start()
    try:
        client.retrieve(dataset, request, target)
    finally:
        stop.set()


if __name__ == "__main__":
    print(
        "[CDS] 说明：网页若出现 “Queued: 几千” 是服务端排队，不是脚本死掉；\n"
        "      已改为按「年-月」合并多日，一次 retrieve 减少排队任务数。"
    )

    date_intervals = [
        ("2024-07-22", "2024-07-26"),
        ("2025-08-22", "2025-08-26"),
        ("2023-07-27", "2023-08-01"),
    ]

    batches: list[tuple[int, int, list[str]]] = []
    for start_str, end_str in date_intervals:
        dates = _expand_interval(start_str, end_str)
        batches.extend(_group_by_year_month(dates))

    for year, month, days in batches:
        ym = f"{year:04d}{month:02d}"
        d_first, d_last = days[0], days[-1]
        target_dir = os.path.join(CFG["root_dir"], str(year), ym)
        os.makedirs(target_dir, exist_ok=True)
        fname = f"era5_{ym}{d_first}_{ym}{d_last}_tp.grib"
        fpath = os.path.join(target_dir, fname)

        label = f"{year}-{month:02d} 共 {len(days)} 天: {days[0]}..{days[-1]}"
        print(f"\n[BATCH] {label} | 文件: {fpath}", flush=True)
        if Path(fpath).exists() and Path(fpath).stat().st_size > 0:
            print(f"  [EXIST] {fname} 已存在，跳过", flush=True)
            continue

        print(
            "  [CDS] 已提交批量请求。Accepted = 在队列里等算力，此时本地还不会开始下文件；"
            "排到 Running/Successful 后才会出现下载进度。",
            flush=True,
        )
        try:
            request = build_era5_request_batch(year, month, days)
            _cds_retrieve_with_heartbeat(CFG["dataset"], request, fpath)
            print(f"  [DONE] {fname} 下载完成", flush=True)
        except Exception as e:
            print(f"  [ERROR] {fname} 下载失败：{e}", flush=True)
            if Path(fpath).exists() and Path(fpath).stat().st_size == 0:
                Path(fpath).unlink(missing_ok=True)

    print("\n[FINISH] 历史批次数据处理完成！", flush=True)
