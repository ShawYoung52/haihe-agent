"""风险接口 /hhfw/riskWarnNew/findDataListByConfig 诊断脚本（服务器侧）。

用途：项目里没有该接口的接入样本，字段名（等级/经纬度）未锁定。
在**服务器**上跑本脚本，把三类风险的真实 body 结构 + 规范化结果打印出来，用于确认：

  1. 等级字段名（riskLevel/grade/levelName/fxdj 等）与取值（中文「一级」/颜色/数字）；
  2. 数字等级映射是否 `1=一级`（最重）——若非，改 risk_warning_tool 的
     `_NUMERIC_LEVEL_MAP` 即可；
  3. 经纬度字段名（lon/lng/x、lat/y）。

用法（服务器，MCP 包根目录）：
    python custom_tools/diagnose_risk_api.py [--kind geologic|mountain|river] [--offline]

- 不传 --kind：依次打印三类。
- --offline：只验证模块加载与打印逻辑，不发请求（本地无内网时用）。

取数口径与 risk_warning_tool 完全一致（`RISK_WARN_BASE`/`RISK_WARN_BASES`/`HHFW_API_BASE`
env 可覆盖，默认同工具；不写死新地址）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

_MCP_ROOT = Path(__file__).resolve().parents[1]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))


def _load_module(name: str, path: Path) -> ModuleType:
    """按路径加载模块并注册进 sys.modules（同 _load_risk_warning_tool 的套路）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_risk_warning_tool():
    """按文件路径加载 risk_warning_tool，绕开 custom_tools/__init__.py 重依赖链。"""
    # 给 stub 的 custom_tools 包注册真实子模块，使
    # `from custom_tools._ttl_cache import make_ttl_cache` 不触发包 __init__。
    pkg = ModuleType("custom_tools")
    pkg.__path__ = []
    sys.modules.setdefault("custom_tools", pkg)

    _load_module("custom_tools._ttl_cache", _MCP_ROOT / "custom_tools" / "_ttl_cache.py")
    return _load_module("risk_warning_tool", _MCP_ROOT / "custom_tools" / "risk_warning_tool.py")


def _describe_payload(payload):
    if isinstance(payload, dict):
        return {
            "top_level_type": "dict",
            "top_level_keys": list(payload.keys())[:20],
        }
    if isinstance(payload, list):
        return {"top_level_type": "list", "length": len(payload)}
    return {"top_level_type": type(payload).__name__, "preview": str(payload)[:200]}


def _field_map(records):
    """统计前若干条记录里出现的键名（用于锁定字段名）。"""
    keys: list[str] = []
    seen = set()
    for row in records:
        if not isinstance(row, dict):
            continue
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


def diagnose(tool, kind: str, offline: bool) -> int:
    cfg = tool.RISK_CONFIGS[kind]
    print("=" * 72)
    print(f"[{kind}] {cfg['label']}  model={cfg['model']} type={cfg['type']}")
    print("=" * 72)
    if offline:
        print("(offline) 跳过接口请求。")
        print("规范化记录字段键：")
        print(" ", tuple(tool._normalize_record({}).keys()))
        return 0

    try:
        payload = tool._fetch_risk_warning(kind, {})
    except Exception as exc:
        print("!! 取数失败:", exc)
        return 1

    print("== 原始 body 顶层结构 ==")
    print(json.dumps(_describe_payload(payload), ensure_ascii=False, indent=2))
    if isinstance(payload, dict) and "raw" in payload:
        print("== 非 JSON 响应原文（前 500 字符）==")
        print(str(payload["raw"])[:500])
        return 0

    records = tool._extract_items(payload)
    print(f"== 记录数: {len(records)} ==")
    if not records:
        print("（空列表，无法锁定字段名）")
        return 0

    print("== 首条记录字段键 ==")
    print(json.dumps(_field_map(records), ensure_ascii=False, indent=2))
    print("== 前 3 条原始记录 ==")
    print(json.dumps(records[:3], ensure_ascii=False, indent=2))

    print("== 规范化结果（前 3 条）+ 等级归一 ==")
    norm = [tool._normalize_record(r) for r in records[:3]]
    for r in norm:
        lv_raw = r.get("level")
        print(
            f"  id={r.get('id')!r} name={r.get('name')!r} area={r.get('area')!r} "
            f"lon={r.get('longitude')!r} lat={r.get('latitude')!r} "
            f"level_raw={lv_raw!r} -> level_norm={tool._normalize_risk_level(lv_raw)!r}"
        )
    print("== 等级归一方向说明 ==")
    print("  数字等级按『越大越高』：5→一级(红最高)…2→四级；1≈无/极低风险。")
    print("  若真实分布方向相反，改 risk_warning_tool 的 _NUMERIC_LEVEL_MAP 即可。")

    # 输出规范化后的等级取值分布（判断 1=一级 假设是否成立）
    level_values = sorted(
        {tool._normalize_risk_level(tool._normalize_record(r).get("level")) for r in records}
    )
    print("== 全量记录归一后的等级取值分布 ==")
    print(json.dumps(level_values, ensure_ascii=False, indent=2))

    # 预览 enrich 后新增字段（表查询失败会静默降级，不影响打印）
    try:
        all_records = [tool._normalize_record(x) for x in records]
        result = tool._summarize(kind, payload)
        result = tool._enrich_risk_result(result, kind, all_records)
        print("== enrich 后新增字段（county_totals / county_risk_summary / hazard_match）==")
        print(json.dumps(
            {
                "county_totals": result.get("county_totals"),
                "county_risk_summary": result.get("county_risk_summary"),
                "hazard_match": result.get("hazard_match"),
                "level_advice_head": (result.get("level_advice") or [])[:2],
            },
            ensure_ascii=False, indent=2,
        ))
    except Exception as exc:
        print("!! enrich 预览失败（不影响上面诊断）:", exc)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="风险接口字段名诊断")
    parser.add_argument("--kind", choices=["geologic", "mountain", "river"], default=None)
    parser.add_argument("--offline", action="store_true", help="不请求接口，只验证加载与打印")
    args = parser.parse_args()

    tool = _load_risk_warning_tool()
    kinds = [args.kind] if args.kind else ["geologic", "mountain", "river"]
    code = 0
    for kind in kinds:
        code = max(code, diagnose(tool, kind, args.offline))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
