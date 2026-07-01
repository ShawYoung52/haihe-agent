#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描 EC 按日目录（支持两种路径）：
  • .../EC_AIFS/{年}/{YYYYMMDD}/
  • .../EC_AIFS/2026/{YYYYMMDD}/  （--ec-root 指到「年」文件夹时以往会扫不到，现已支持）

用法（在项目根执行）：
  python scripts/enqueue_all_ec_forecast_products.py --config config.ini
  python scripts/enqueue_all_ec_forecast_products.py --ec-root /home/ev/data/ec/EC_AIFS --list-days
  python scripts/enqueue_all_ec_forecast_products.py --ec-root /home/ev/data/ec/EC_AIFS/2026 --dry-run
  python scripts/enqueue_all_ec_forecast_products.py --ec-root .../2026 --from-ymd 20260309 --dry-run

注意：
• 不带 --http-base-url：在本进程调用 enqueue_forecast_product_job（会启动内存里的单线程 worker，脚本末尾会等队列排空）。
• 带 --http-base-url：只向已运行的 emergency_http_server 发 POST（与你在接口里点的队列是同一条），本脚本不等待出图完成。

若 --list-days 报错「unrecognized arguments」，说明服务器上的脚本不是最新版，请同步本仓库后再跑。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import List, Optional, Set, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from forecast_product_queue import (  # noqa: E402
    enqueue_forecast_product_job,
    wait_forecast_product_queue_idle,
    _read_default_paths,
)
from haihe_mcp_tools import BusinessException, collect_ec_forecast_precip_files  # noqa: E402


def _parse_cycle_from_ec_filename(name: str) -> Optional[str]:
    # GRIB：必须与 -{N}h-oper-fc.grib2 紧邻，避免误匹配文件名其它位置的数字串
    m = re.match(r"(20\d{12})-\d+h-oper-fc\.grib2$", name, re.I)
    if m:
        return m.group(1)[:10]
    m10 = re.match(r"(20\d{10})-\d+h-oper-fc\.grib2$", name, re.I)
    if m10:
        return m10.group(1)[:10]
    m2 = re.match(r"ec_(20\d{8})(\d{2})_.*\.(tif|tiff)$", name, re.I)
    if m2:
        return m2.group(1) + m2.group(2)
    return None


def _iter_ec_day_dirs(ec_root: str) -> List[Tuple[str, str]]:
    """
    返回 [(当日目录绝对路径, YYYYMMDD), ...]。
    ec_root 为 EC_AIFS 根：遍历 年/YYYYMMDD。
    ec_root 已为年目录（如 .../2026）：子目录直接为 YYYYMMDD（勿把该层当成「年」再往下找）。
    """
    out: List[Tuple[str, str]] = []
    ec_root = os.path.abspath(ec_root)
    if not os.path.isdir(ec_root):
        return out
    base = os.path.basename(ec_root)
    if len(base) == 4 and base.isdigit():
        for ymd in sorted(os.listdir(ec_root)):
            if not (len(ymd) == 8 and ymd.isdigit()):
                continue
            day_path = os.path.join(ec_root, ymd)
            if os.path.isdir(day_path):
                out.append((day_path, ymd))
        return out
    for year_name in sorted(os.listdir(ec_root)):
        if not (len(year_name) == 4 and year_name.isdigit()):
            continue
        year_path = os.path.join(ec_root, year_name)
        if not os.path.isdir(year_path):
            continue
        for ymd in sorted(os.listdir(year_path)):
            if not (len(ymd) == 8 and ymd.isdigit()):
                continue
            day_path = os.path.join(year_path, ymd)
            if os.path.isdir(day_path):
                out.append((day_path, ymd))
    return out


def _http_post_enqueue(
    base_url: str,
    start_times: List[str],
    hours: Tuple[int, ...],
    ec_path: str,
    config_path: str,
) -> None:
    base = base_url.rstrip("/")
    url = f"{base}/emergency/forecast/products/jobs"
    hours_str = ",".join(str(h) for h in hours)
    cfg_abs = os.path.abspath(config_path)
    ok = 0
    for st in start_times:
        payload = {
            "start_time": st,
            "hours": hours_str,
            "ec_output_path": ec_path,
            "config_path": cfg_abs,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                code = resp.status
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:800]
            print(f"[enqueue-all] HTTP {e.code} start_time={st!r}: {err_body}")
            continue
        except OSError as e:
            print(f"[enqueue-all] 请求失败 start_time={st!r}: {e}")
            continue
        if code == 202:
            try:
                d = json.loads(raw)
                jid = d.get("job_id", "")
                print(f"  HTTP 202 job_id={jid} start_time={st!r}")
            except json.JSONDecodeError:
                print(f"  HTTP 202 非 JSON: {raw[:200]!r}")
            ok += 1
        else:
            print(f"  HTTP {code} {raw[:400]!r}")
    print(
        f"[enqueue-all] HTTP 提交结束 {ok}/{len(start_times)} 个任务 → {url} "
        f"（出图由服务端队列顺序执行，本脚本不等待）"
    )


def _resolve_ec_path_for_collect(
    ec_root_scan: str,
    explicit_output_path: str,
    config_ec_output: str,
) -> tuple[str, str]:
    """
    供 collect / 入队 job 使用的 ec_output_path。
    未传 --ec-output-path 时，用 --ec-root 推出真正的 EC_AIFS 根目录，
    避免继续用 config 里常见的「扁平 tif」路径 …/EC_AIFS/output 当作查找根（语义易混淆）。
    """
    ex = (explicit_output_path or "").strip()
    if ex:
        return ex, "显式 --ec-output-path"
    root = os.path.abspath((ec_root_scan or "").strip())
    if not root or not os.path.isdir(root):
        return config_ec_output, "ec_root 无效，回退 config [paths] ecOutput"
    base = os.path.basename(root.rstrip(os.sep))
    if len(base) == 4 and base.isdigit():
        parent = os.path.dirname(root)
        if parent and os.path.isdir(parent):
            return parent, "由 --ec-root（年目录）取上一级为 EC_AIFS 根"
    return root, "--ec-root 即 EC_AIFS 根"


def _candidate_cycles(ymd: str, day_path: str) -> List[str]:
    found: Set[str] = set()
    try:
        for fn in os.listdir(day_path):
            c = _parse_cycle_from_ec_filename(fn)
            if c and c.startswith(ymd):
                found.add(c)
    except OSError:
        pass
    for hh in ("00", "02", "06", "08", "12", "14", "18", "20"):
        found.add(ymd + hh)
    return sorted(found)


def _filter_days_by_ymd_range(
    days: List[Tuple[str, str]],
    from_ymd: str,
    to_ymd: str,
) -> List[Tuple[str, str]]:
    """按 YYYYMMDD 字符串比较（与时间无关，仅日历序）筛选。"""
    out: List[Tuple[str, str]] = []
    for dp, ymd in days:
        if from_ymd and ymd < from_ymd:
            continue
        if to_ymd and ymd > to_ymd:
            continue
        out.append((dp, ymd))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="扫描 EC 按日目录并入队预报产品图任务")
    ap.add_argument(
        "--ec-root",
        default=os.getenv("EC_AIFS_ROOT", "/home/ev/data/ec/EC_AIFS"),
        help="EC_AIFS 根（…/EC_AIFS 下为 年/YYYYMMDD）或某年目录（…/EC_AIFS/2026 下直接为 YYYYMMDD）",
    )
    ap.add_argument(
        "--list-days",
        action="store_true",
        help="只打印扫描到的日期目录 YYYYMMDD 并退出（用于核对路径）",
    )
    ap.add_argument("--config", default=os.path.join(_ROOT, "config.ini"))
    ap.add_argument(
        "--hours",
        default="12,24,36,48,60,72",
        help="与入队任务 hours 一致，逗号分隔",
    )
    ap.add_argument(
        "--ec-output-path",
        default="",
        help="传给 collect/入队 job 的 EC 根路径；不设则根据 --ec-root 自动推导 EC_AIFS 根（不再默认用 config 里的 …/output）",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将入队的起报，不真正入队",
    )
    ap.add_argument(
        "--http-base-url",
        default=(os.getenv("EMERGENCY_HTTP_BASE") or "").strip(),
        help="例如 http://127.0.0.1:8080：经 HTTP POST /emergency/forecast/products/jobs 入队（与接口共用服务端队列）。也可用环境变量 EMERGENCY_HTTP_BASE。",
    )
    ap.add_argument(
        "--from-ymd",
        default="",
        help="只处理 YYYYMMDD ≥ 该日期的目录（含当日），例如从 2026-03-09 起：20260309",
    )
    ap.add_argument(
        "--to-ymd",
        default="",
        help="只处理 YYYYMMDD ≤ 该日期的目录（含当日）；不设则无上界",
    )
    args = ap.parse_args()

    hours = tuple(int(x) for x in args.hours.split(",") if x.strip().isdigit())
    if not hours:
        hours = (12, 24, 36, 48, 60, 72)

    _, ec_default = _read_default_paths(args.config)
    ec_path, ec_path_note = _resolve_ec_path_for_collect(
        args.ec_root, args.ec_output_path, ec_default
    )

    fy = (args.from_ymd or "").strip()
    ty = (args.to_ymd or "").strip()
    for label, raw in (("--from-ymd", fy), ("--to-ymd", ty)):
        if raw and (len(raw) != 8 or not raw.isdigit()):
            print(f"[enqueue-all] {label} 须为八位数字 YYYYMMDD，当前: {raw!r}")
            sys.exit(2)

    days = _iter_ec_day_dirs(args.ec_root)
    n_before = len(days)
    days = _filter_days_by_ymd_range(days, fy, ty)
    if fy or ty:
        print(
            f"[enqueue-all] 日期筛选 from_ymd={fy or '(不设)'} to_ymd={ty or '(不设)'}："
            f"{n_before} → {len(days)} 个日期目录"
        )
    if args.list_days:
        print(f"[enqueue-all] ec_root={os.path.abspath(args.ec_root)!r} 扫描到 {len(days)} 个日期目录:")
        for _dp, ymd in days:
            print(f"  {ymd}")
        return
    if not days:
        print(
            f"[enqueue-all] 未找到 年/YYYYMMDD 结构。请确认 --ec-root 指向 …/EC_AIFS 或 …/EC_AIFS/2026 ，"
            f"勿单独指向 …/output（扁平 output 下没有日期子目录）。当前: {os.path.abspath(args.ec_root)!r}"
        )
        sys.exit(1)

    ymd_seen = sorted({ymd for _, ymd in days})
    if len(ymd_seen) <= 15:
        ymd_preview = ", ".join(ymd_seen)
    else:
        ymd_preview = ", ".join(ymd_seen[:6]) + f" …(+{len(ymd_seen) - 6}天)… " + ", ".join(ymd_seen[-3:])
    print(
        f"[enqueue-all] ec_root 下共 {len(ymd_seen)} 个 YYYYMMDD 目录: {ymd_preview}。"
        f" 查找 GRIB 使用 ec_output_path={ec_path!r}（{ec_path_note}）。"
    )

    seen_compact: Set[str] = set()
    to_enqueue: List[str] = []

    for day_path, ymd in days:
        for cyc in _candidate_cycles(ymd, day_path):
            try:
                meta = collect_ec_forecast_precip_files(cyc, ec_path, hours)
            except BusinessException:
                continue
            if not any(v for v in (meta.get("ec_files") or {}).values() if v):
                continue
            compact = str(meta.get("start_time_compact") or "")
            if len(compact) < 10 or compact in seen_compact:
                continue
            seen_compact.add(compact)
            to_enqueue.append(meta.get("start_time") or compact)

    ymd_ok = {c[:8] for c in seen_compact}
    missing_ymd = [y for y in ymd_seen if y not in ymd_ok]
    if missing_ymd:
        show = missing_ymd if len(missing_ymd) <= 25 else missing_ymd[:25] + ["…"]
        print(
            f"[enqueue-all] 下列日期有文件夹但 **未找到** 与时效 {hours} 匹配的降水文件（命名须符合 haihe_mcp_tools 规则）: {show}"
        )

    print(f"[enqueue-all] 将处理起报数: {len(to_enqueue)}（每个起报对应一个队列任务，任务内画多时效），时效 {hours}，ec_output_path={ec_path}")
    if days and len(to_enqueue) == 0:
        print(
            f"[enqueue-all] 提示: 已扫到 {len(days)} 个日期目录但无起报可入队；"
            f"请确认各日目录内有 *-{{12,24,36,48,60,72}}h-oper-fc.grib2（或 ec_*_rain_total_*h.tif），且 ec_output_path/EC_AIFS_ROOT 与数据布局一致。"
        )

    if args.dry_run:
        http_hint = (args.http_base_url or "").strip()
        for st in to_enqueue:
            print(f"  dry-run  {st}" + (f"  → POST {http_hint}/emergency/forecast/products/jobs" if http_hint else ""))
        return

    http_base = (args.http_base_url or "").strip().rstrip("/")
    if http_base:
        _http_post_enqueue(http_base, to_enqueue, hours, ec_path, args.config)
        return

    for st in to_enqueue:
        job = enqueue_forecast_product_job(
            start_time=st,
            hours=hours,
            ec_output_path=ec_path,
            config_path=args.config,
        )
        print(f"  已入队 job_id={job.job_id} start_time={st!r}")

    print("[enqueue-all] 等待本进程队列排空…")
    wait_forecast_product_queue_idle()
    print("[enqueue-all] 全部完成。")


if __name__ == "__main__":
    main()
