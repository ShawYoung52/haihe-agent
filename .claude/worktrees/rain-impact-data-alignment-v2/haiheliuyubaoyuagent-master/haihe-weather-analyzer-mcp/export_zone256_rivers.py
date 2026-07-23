from __future__ import annotations
import argparse
import json
import os
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _clean_text(s: str) -> str:
    # 移除 JSON 解析易出问题的控制字符，保留换行/回车/制表
    return "".join(ch for ch in s if ch >= " " or ch in "\n\r\t")


def _sanitize_obj(obj):
    if isinstance(obj, dict):
        return {str(k): _sanitize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_obj(v) for v in obj]
    if isinstance(obj, str):
        return _clean_text(obj)
    return obj


def fetch_json(url: str, timeout_sec: int = 30):
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    return _sanitize_obj(payload)

def parse_times_list(times_csv: str):
    arr = [x.strip() for x in str(times_csv or "").split(",") if x.strip()]
    if not arr:
        raise ValueError("times-list 不能为空，例如 20230730000000,20230730060000")
    return arr

def main():
    ap = argparse.ArgumentParser(description="导出 246/256 分区影响河系场景数据")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--times-list", required=True, help="逗号分隔时次，例：20230730000000,20230730060000")
    ap.add_argument("--scope", default="haihe")
    ap.add_argument("--map-render", default="wms_sql", help="wms_sql 或 geojson")
    ap.add_argument("--local-json-path", default="", help="可选：merged_6h_all_ranges.json 路径")
    ap.add_argument("--force-local", action="store_true", help="是否强制走本地 merged")
    ap.add_argument("--max-rivers", type=int, default=800)
    ap.add_argument("--timeout-sec", type=int, default=30)
    ap.add_argument("--output-file", required=True)
    ap.add_argument(
        "--ascii-json",
        action="store_true",
        help="输出 ASCII 转义 JSON（跨平台更稳定，推荐给 Windows PowerShell）",
    )
    args = ap.parse_args()

    times_list = parse_times_list(args.times_list)
    slots = []
    ok = 0
    fail = 0

    for t in times_list:
        params = {
            "times": t,
            "scope": args.scope,
            "map_render": args.map_render,
            "max_rivers": args.max_rivers,
        }
        if args.force_local:
            params["force_local"] = "true"
        if args.local_json_path:
            params["local_json_path"] = args.local_json_path

        q = urlencode(params)
        url = f"{args.base_url.rstrip('/')}/scenario/emergency/zone256-rivers?{q}"
        one = {"times": t, "url": url}
        try:
            payload = fetch_json(url, timeout_sec=max(1, args.timeout_sec))
            one["ok"] = True
            one["payload"] = payload
            ok += 1
        except Exception as e:
            one["ok"] = False
            one["error"] = str(e)
            fail += 1
        slots.append(one)

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {
            "base_url": args.base_url,
            "scope": args.scope,
            "map_render": args.map_render,
            "count_total": len(times_list),
            "count_ok": ok,
            "count_fail": fail,
        },
        "slots": slots,
    }

    os.makedirs(os.path.dirname(args.output_file) or ".", exist_ok=True)
    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=bool(args.ascii_json), indent=2)

    print(json.dumps({
        "ok": True,
        "output_file": args.output_file,
        "count_total": len(times_list),
        "count_ok": ok,
        "count_fail": fail
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()