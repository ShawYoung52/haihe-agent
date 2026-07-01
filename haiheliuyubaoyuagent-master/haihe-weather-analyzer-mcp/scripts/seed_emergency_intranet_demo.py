#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向 config.ini 中 [postgres] 所指向的库（通常为测试库）写入应急「演示数据」，供前端联调：

  • 应急管理主表 hh_emergency_event  → GET /emergency/management/events 及详情、
    GET /emergency/management/response-board（四象限列表+时间轴+问答提示，供 Ajax 轮询）
  • 判定流水表 haihe_emergency_event（或 config 中 emergency_event_table）→ GET /emergency/events 及详情

在项目根目录执行（读取 config.ini 的 [postgres]）：

  python scripts/seed_emergency_intranet_demo.py
  python scripts/seed_emergency_intranet_demo.py --dry-run

幂等：固定 event_code / event_id，重复执行不会重复插入（ON CONFLICT DO NOTHING）。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from psycopg2.extras import Json

from emergency_event_store import EmergencyEventStore
from emergency_management_store import EmergencyManagementStore


def _demo_management_rows(now: datetime) -> List[Dict[str, Any]]:
    t0 = now - timedelta(days=2)
    t1 = now - timedelta(days=10)
    end1 = t1 + timedelta(days=3)
    t_closed = now - timedelta(days=18)
    end_closed = t_closed + timedelta(days=5)
    t_old = now - timedelta(days=45)
    end_old = t_old + timedelta(days=12)
    return [
        {
            "event_code": "DEMO-EVT-ACTIVE-202604",
            "event_type": "rainstorm",
            "event_level": "III",
            "title": "【演示】海河流域暴雨三级应急响应（持续中）",
            "status": "active",
            "start_time": t0.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {
                "demo": True,
                "note": "测试库·前端表格联调用",
                "zone_codes": ["Z01", "Z02"],
                "contact_dept": "气象台值班室",
            },
        },
        {
            "event_code": "DEMO-EVT-ARCHIVED-202603",
            "event_type": "rainstorm",
            "event_level": "IV",
            "title": "【演示】海河流域暴雨四级应急响应（已归档）",
            "status": "archived",
            "start_time": t1.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end1.strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {"demo": True, "archived_reason": "演示归档", "summary": "过程雨量减弱，解除响应。"},
        },
        {
            "event_code": "DEMO-EVT-TYPHOON-202604",
            "event_type": "typhoon",
            "event_level": "II",
            "title": "【演示】台风影响海河流域二级响应（持续中）",
            "status": "active",
            "start_time": (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {"demo": True, "typhoon_name": "演示台风-2026", "landfall_province": "闽"},
        },
        {
            "event_code": "DEMO-EVT-LEVEL-I-202604",
            "event_type": "rainstorm",
            "event_level": "I",
            "title": "【演示】极端暴雨一级应急响应（持续中·慎用样式）",
            "status": "active",
            "start_time": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {"demo": True, "severity_hint": "最高等级，用于前端高亮/徽章联调"},
        },
        {
            "event_code": "DEMO-EVT-IV-ACTIVE-SMALL",
            "event_type": "rainstorm",
            "event_level": "IV",
            "title": "【演示】流域面降水偏弱·四级响应（持续中）",
            "status": "active",
            "start_time": (now - timedelta(days=1, hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {"demo": True, "basin_avg_mm_24h": 28.5},
        },
        {
            "event_code": "DEMO-EVT-FLOOD-RIVER-202604",
            "event_type": "flood",
            "event_level": "III",
            "title": "【演示】河道洪水风险·三级响应（持续中）",
            "status": "active",
            "start_time": (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {"demo": True, "river_focus": ["永定河", "大清河"], "water_level_note": "部分站点超警戒演示数据"},
        },
        {
            "event_code": "DEMO-EVT-CLOSED-202602",
            "event_type": "rainstorm",
            "event_level": "III",
            "title": "【演示】已关闭（closed）状态的三级暴雨响应",
            "status": "closed",
            "start_time": t_closed.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_closed.strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {"demo": True, "status_note": "closed 与 archived 在前端均显示为已归档"},
        },
        {
            "event_code": "DEMO-EVT-ENDED-202601",
            "event_type": "typhoon",
            "event_level": "IV",
            "title": "【演示】已结束（ended）台风四级响应",
            "status": "ended",
            "start_time": (t_closed - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (end_closed - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {"demo": True},
        },
        {
            "event_code": "DEMO-EVT-OLD-ARCHIVED-202501",
            "event_type": "rainstorm",
            "event_level": "II",
            "title": "【演示】历史归档条目（用于分页/时间筛选）",
            "status": "archived",
            "start_time": t_old.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_old.strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {
                "demo": True,
                "tags": ["分页测试", "时间轴"],
                "operator": "system_seed",
            },
        },
        {
            "event_code": "DEMO-EVT-LONG-TITLE-FOR-UI-WRAP-TEST-001",
            "event_type": "rainstorm",
            "event_level": "IV",
            "title": "【演示】超长标题：海河流域××区段短时强降水与雷暴大风叠加之四级应急响应（用于表格换行/省略号/tooltip 联调）",
            "status": "active",
            "start_time": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {"demo": True, "ui_hint": "长文本列展示"},
        },
        # 大屏四象限（过去 / 现在 / 正在发生 / 几小时后）各一条虚构数据，对应 GET /emergency/management/response-board
        {
            "event_code": "DEMO-TIMELINE-PAST-001",
            "event_type": "rainstorm",
            "event_level": "IV",
            "title": "【演示·过去】沧州曾发布暴雨预警（过程已结束）",
            "status": "archived",
            "start_time": (now - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now - timedelta(hours=11)).strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {
                "demo": True,
                "response_ui": {
                    "dialog_title": "沧州预警（已结束）",
                    "dialog_body": "过去时段内沧州地区曾发布暴雨黄色预警，过程已结束，可用于复盘。",
                    "list_summary": "沧州·预警解除",
                    "hint": "时间轴「过去」区可点此条目查看摘要。",
                },
            },
        },
        {
            "event_code": "DEMO-TIMELINE-NOW-001",
            "event_type": "rainstorm",
            "event_level": "II",
            "title": "【演示·现在】天津武清刚进入暴雨高风险关注时段",
            "status": "active",
            "start_time": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": None,
            "ext": {
                "demo": True,
                "response_ui": {
                    "dialog_title": "武清·当前态势",
                    "dialog_body": "监测显示武清区降水强度快速增强，刚进入预案关注的暴雨高风险窗口，请加密会商。",
                    "list_summary": "武清区·高风险窗口起始",
                    "hint": "对应时间轴「现在」刻度左右的高亮节点。",
                },
            },
        },
        {
            "event_code": "DEMO-TIMELINE-ONGOING-001",
            "event_type": "rainstorm",
            "event_level": "III",
            "title": "【演示·正在发生】海河流域面降水持续·三级响应生效中",
            "status": "active",
            "start_time": (now - timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {
                "demo": True,
                "response_ui": {
                    "dialog_title": "流域面降水过程",
                    "dialog_body": "流域平均面雨量维持较高水平，三级应急响应持续生效，请关注河道与水库调度。",
                    "list_summary": "全流域·三级响应",
                    "hint": "时间轴「正在发生」区段展示进行中的过程。",
                },
            },
        },
        {
            "event_code": "DEMO-TIMELINE-FUTURE-001",
            "event_type": "rainstorm",
            "event_level": "II",
            "title": "【演示·几小时后】预报：天津将进入二级应急响应",
            "status": "active",
            "start_time": (now + timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S"),
            "ext": {
                "demo": True,
                "effective_start_time": (now + timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S"),
                "effective_end_time": (now + timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S"),
                "response_ui": {
                    "dialog_title": "天津·预报节点",
                    "dialog_body": "根据数值预报与预案推演，预计十余小时后天津片区可能达到二级响应启动条件（演示虚构）。",
                    "list_summary": "天津·预报二级响应节点",
                    "hint": "时间轴「未来」侧卡片，可与图层 24/48/72h 雨量联动。",
                },
            },
        },
    ]


def _demo_flow_rows(now: datetime) -> List[Dict[str, Any]]:
    def _ts(**kw: Any) -> str:
        return (now - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")

    obs_evidence_rich = {
        "top_stations": [
            {"Station_Id_C": "54527", "name": "演示站A", "rain_mm": 82.4, "Lat": 39.12, "Lon": 117.20},
            {"Station_Id_C": "54528", "name": "演示站B", "rain_mm": 76.1, "Lat": 38.95, "Lon": 116.45},
            {"Station_Id_C": "54529", "name": "演示站C", "rain_mm": 61.0, "Lat": 39.45, "Lon": 115.88},
        ],
        "basin_codes": "HHLY",
        "window_hours": 3,
    }
    ec_partial = {
        "6h": "/demo/ec/accum_6h.grib2",
        "12h": "/demo/ec/accum_12h.grib2",
        "24h": "/demo/ec/accum_24h.grib2",
        "48h": "/demo/ec/accum_48h.grib2",
        "72h": "/demo/ec/accum_72h.grib2",
    }

    return [
        {
            "event_id": "demo_flow_obs_triggered_0001",
            "event_time": _ts(hours=1),
            "event_type": "observation",
            "status": "triggered",
            "level": "III",
            "message": "【演示】满足III级应急响应条件（实况口径）",
            "request": {
                "times": "20260403080000",
                "basin_codes": "HHLY",
                "neighbor_km": 50,
                "allowed_station_levels": "",
                "demo": True,
            },
            "response": {
                "reached": True,
                "level": "III",
                "message": "【演示】满足III级应急响应条件（实况口径）",
                "evidence": {**obs_evidence_rich, "demo_stations_mm": 45.2},
            },
            "products": [],
        },
        {
            "event_id": "demo_flow_fc_not_triggered_0002",
            "event_time": _ts(hours=3),
            "event_type": "forecast",
            "status": "not_triggered",
            "level": None,
            "message": "【演示】当前未满足 I/II/III/IV 级应急响应条件（EC预报口径）",
            "request": {
                "start_time": "2026040302",
                "basin_codes": "HHLY",
                "ec_output_path": "/demo/EC_AIFS",
                "demo": True,
            },
            "response": {
                "reached": False,
                "level": None,
                "message": "【演示】当前未满足 I/II/III/IV 级应急响应条件（EC预报口径）",
                "evidence": {"ec_files": {"12h": ec_partial["12h"], "24h": ec_partial["24h"]}},
            },
            "products": [
                {
                    "product_id": "ec_12h",
                    "product_type": "forecast_grib",
                    "title": "EC预报累计降水 12h",
                    "path": ec_partial["12h"],
                },
                {
                    "product_id": "ec_24h",
                    "product_type": "forecast_grib",
                    "title": "EC预报累计降水 24h",
                    "path": ec_partial["24h"],
                },
            ],
        },
        {
            "event_id": "demo_flow_obs_not_triggered_0003",
            "event_time": _ts(hours=5),
            "event_type": "observation",
            "status": "not_triggered",
            "level": None,
            "message": "【演示】实况未达阈值（not_triggered）",
            "request": {"times": "20260403020000", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": False,
                "level": None,
                "message": "【演示】实况未达阈值",
                "evidence": {"max_station_mm": 8.2, "station_count": 412},
            },
            "products": [],
        },
        {
            "event_id": "demo_flow_obs_triggered_iv_0004",
            "event_time": _ts(hours=8),
            "event_type": "observation",
            "status": "triggered",
            "level": "IV",
            "message": "【演示】满足IV级应急响应条件（实况口径）",
            "request": {"times": "20260402120000", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": True,
                "level": "IV",
                "message": "【演示】满足IV级应急响应条件（实况口径）",
                "evidence": obs_evidence_rich,
            },
            "products": [],
        },
        {
            "event_id": "demo_flow_obs_triggered_ii_0005",
            "event_time": _ts(days=1, hours=2),
            "event_type": "observation",
            "status": "triggered",
            "level": "II",
            "message": "【演示】满足II级应急响应条件（实况口径）",
            "request": {"times": "20260401180000", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": True,
                "level": "II",
                "message": "【演示】满足II级应急响应条件（实况口径）",
                "evidence": {"threshold_breach": "multi-station sustained", "stations_over_50mm": 7},
            },
            "products": [],
        },
        {
            "event_id": "demo_flow_fc_triggered_iii_0006",
            "event_time": _ts(days=1, hours=6),
            "event_type": "forecast",
            "status": "triggered",
            "level": "III",
            "message": "【演示】满足III级应急响应条件（EC预报口径）",
            "request": {"start_time": "2026040108", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": True,
                "level": "III",
                "message": "【演示】满足III级应急响应条件（EC预报口径）",
                "evidence": {"ec_files": ec_partial, "grid_max_mm_24h": 95.0},
            },
            "products": [
                {"product_id": f"ec_{k}", "product_type": "forecast_grib", "title": f"EC预报累计降水 {k}", "path": v}
                for k, v in ec_partial.items()
            ],
        },
        {
            "event_id": "demo_flow_fc_triggered_iv_typhoon_0007",
            "event_time": _ts(days=2),
            "event_type": "forecast",
            "status": "triggered",
            "level": "IV",
            "message": "【演示】满足IV级应急响应条件（预报口径：台风将影响海河流域）",
            "request": {"start_time": "2026033012", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": True,
                "level": "IV",
                "message": "【演示】预报台风路径进入警戒区",
                "evidence": {"typhoon_demo_id": "TD-2026-DEMO", "ec_files": {"48h": ec_partial["48h"], "72h": ec_partial["72h"]}},
            },
            "products": [
                {
                    "product_id": "ec_48h",
                    "product_type": "forecast_grib",
                    "title": "EC预报累计降水 48h",
                    "path": ec_partial["48h"],
                },
                {
                    "product_id": "ec_72h",
                    "product_type": "forecast_grib",
                    "title": "EC预报累计降水 72h",
                    "path": ec_partial["72h"],
                },
            ],
        },
        {
            "event_id": "demo_flow_obs_triggered_i_0008",
            "event_time": _ts(days=3),
            "event_type": "observation",
            "status": "triggered",
            "level": "I",
            "message": "【演示】满足I级应急响应条件（实况口径·极端）",
            "request": {"times": "20260329060000", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": True,
                "level": "I",
                "message": "【演示】满足I级应急响应条件（实况口径·极端）",
                "evidence": {"extreme_hourly_mm": 52.0, "affected_counties": 6},
            },
            "products": [],
        },
        {
            "event_id": "demo_flow_fc_partial_files_0009",
            "event_time": _ts(days=4),
            "event_type": "forecast",
            "status": "not_triggered",
            "level": None,
            "message": "【演示】部分时效栅格缺失时的 evidence 形态",
            "request": {"start_time": "2026032800", "basin_codes": "HHLY", "demo": True},
            "response": {
                "reached": False,
                "level": None,
                "message": "【演示】未达阈值且 72h 文件为空",
                "evidence": {"ec_files": {"12h": ec_partial["12h"], "24h": None, "72h": None}},
            },
            "products": [
                {
                    "product_id": "ec_12h",
                    "product_type": "forecast_grib",
                    "title": "EC预报累计降水 12h",
                    "path": ec_partial["12h"],
                }
            ],
        },
        {
            "event_id": "demo_flow_long_message_0010",
            "event_time": _ts(days=6),
            "event_type": "observation",
            "status": "not_triggered",
            "level": None,
            "message": "【演示】较长说明文字：本次实况时次流域内国家站与区域站均已参与质控；邻站合并半径 50km；持续降水阈值 0.1mm/h；"
            "综合判定未同时满足面降水与单站极端条件，故未启动应急响应。本段用于详情页换行与复制按钮联调。",
            "request": {
                "times": "20260325080000",
                "basin_codes": "HHLY",
                "sustain_hourly_threshold_mm": 0.1,
                "include_evidence": True,
                "demo": True,
            },
            "response": {
                "reached": False,
                "level": None,
                "message": "【演示】较长说明文字：本次实况时次流域内国家站与区域站均已参与质控；邻站合并半径 50km；持续降水阈值 0.1mm/h；"
                "综合判定未同时满足面降水与单站极端条件，故未启动应急响应。本段用于详情页换行与复制按钮联调。",
                "evidence": {"qc_flags": ["range_ok", "time_ok"], "stations_used": 520},
            },
            "products": [],
        },
    ]


def main() -> None:
    os.chdir(_ROOT)
    parser = argparse.ArgumentParser(description="应急管理演示数据种子（写入测试库）")
    parser.add_argument(
        "--config",
        default=os.path.join(_ROOT, "config.ini"),
        help="config.ini 路径",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的行数，不连库")
    args = parser.parse_args()

    now = datetime.now()
    mgmt_rows = _demo_management_rows(now)
    flow_rows = _demo_flow_rows(now)

    if args.dry_run:
        print(f"dry-run: 将写入 hh_emergency_event {len(mgmt_rows)} 条, 流水表 {len(flow_rows)} 条")
        return

    mgmt = EmergencyManagementStore(args.config)
    flow_store = EmergencyEventStore(args.config)
    schema = mgmt.schema
    flow_table = flow_store.table

    sql_mgmt = f"""
    INSERT INTO {schema}.hh_emergency_event
        (event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext)
    VALUES
        (%(event_code)s, %(event_type)s, %(event_level)s, %(title)s, %(status)s,
         %(start_time)s::timestamp, %(end_time)s::timestamp, NULL, %(ext)s::jsonb)
    ON CONFLICT (event_code) DO NOTHING;
    """

    sql_flow = f"""
    INSERT INTO {schema}.{flow_table}
        (event_id, event_time, event_type, status, level, message, created_at,
         request_json, response_json, products_json)
    VALUES
        (%(event_id)s, %(event_time)s::timestamp, %(event_type)s, %(status)s, %(level)s, %(message)s,
         NOW(), %(request_json)s, %(response_json)s, %(products_json)s)
    ON CONFLICT (event_id) DO NOTHING;
    """

    with mgmt._connect() as conn:
        with conn.cursor() as cur:
            for row in mgmt_rows:
                cur.execute(
                    sql_mgmt,
                    {
                        **row,
                        "ext": Json(row["ext"]),
                    },
                )
            for row in flow_rows:
                cur.execute(
                    sql_flow,
                    {
                        "event_id": row["event_id"],
                        "event_time": row["event_time"],
                        "event_type": row["event_type"],
                        "status": row["status"],
                        "level": row["level"],
                        "message": row["message"],
                        "request_json": Json(row["request"]),
                        "response_json": Json(row["response"]),
                        "products_json": Json(row["products"]),
                    },
                )
        conn.commit()

    print(
        "完成：已尝试写入应急管理 "
        f"{len(mgmt_rows)} 条（hh_emergency_event）、流水 "
        f"{len(flow_rows)} 条（{schema}.{flow_table}）；已存在的 demo 主键会被跳过。"
    )
    print("联调示例：")
    print("  GET /emergency/management/events?page_size=50&status=active")
    print("  GET /emergency/management/response-board?history_hours=48&future_hours=72")
    print("  GET /emergency/management/events/DEMO-EVT-LEVEL-I-202604")
    print("  GET /emergency/events?page_size=50&event_type=forecast")
    print("  GET /emergency/events/demo_flow_fc_triggered_iii_0006")
    print("  GET /emergency/events/demo_flow_fc_triggered_iii_0006/products")


if __name__ == "__main__":
    main()
