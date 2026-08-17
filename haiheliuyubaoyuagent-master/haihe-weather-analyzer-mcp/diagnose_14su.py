"""14所接口聚焦诊断脚本（第四轮，内网运行）。

目标：验证"预报接口 500 根因 = 预报窗口用了实况窗口（beginTime 早于 foreTime）"的修复假设：
用 foreTime==beginTime（起报对齐）的窗口测 fore_img / forecast；并连续 3 次调
area_rain_real_img 确认它是否稳定 200（组合长图里曾"未加载成功"）。

运行：python diagnose_14su.py   （需要 requests）
"""
import requests
from datetime import datetime, timedelta

HOST = "10.226.107.35"
BASE = f"http://{HOST}:8001"
AREA_IDS = [6, 7, 8, 9, 10, 11, 12, 13, 14]
now = datetime.now()
END = now.replace(minute=0, second=0, microsecond=0)
B = (END - timedelta(hours=24)).strftime("%Y-%m-%d %H:00:00")
E = END.strftime("%Y-%m-%d %H:00:00")
FT = "2026-08-17 08:00:00"   # 今天 08 时起报
FE = "2026-08-18 08:00:00"   # 未来 24h


def probe(label, url, payload, times=1):
    print(f"\n===== {label} =====")
    for i in range(times):
        try:
            r = requests.post(url, json=payload, timeout=60)
            ok = r.status_code
            head = r.text[:120]
            try:
                d = r.json().get("data")
                if isinstance(d, str):
                    head = f"data=str len={len(d)} head={d[:60]!r}"
                elif isinstance(d, list):
                    head = f"data=list len={len(d)}"
            except Exception:
                pass
            print(f"  [{i}] HTTP {ok} | {head}")
        except Exception as e:
            print(f"  [{i}] EXC {type(e).__name__}: {e}")


# 【1】area_rain_real_img 连续 3 次（间歇 500 检测）
probe("area_rain_real_img ×3（实况窗口）",
      f"{BASE}/openapi/meteor_img/area_rain_real_img?forceCreate=1",
      {"areaIds": AREA_IDS, "beginTime": B, "endTime": E, "interval": 24, "range": "9", "type": "0", "isClimateImg": False},
      times=3)

# 【2】area_rain_fore_img：起报对齐窗口（foreTime==beginTime）
probe("area_rain_fore_img（foreTime==beginTime 对齐）",
      f"{BASE}/openapi/meteor_img/area_rain_fore_img?forceCreate=1",
      {"areaIds": AREA_IDS, "foreTime": FT, "beginTime": FT, "endTime": FE,
       "intval": 24, "modelTypes": ["ECMF"], "range": "9", "isClimateImg": False})

# 【3】area_rain_fore_img：用昨天的 foreTime（8-16 08:00）试一次（若今天时次未起报）
probe("area_rain_fore_img（foreTime=昨天08时）",
      f"{BASE}/openapi/meteor_img/area_rain_fore_img?forceCreate=1",
      {"areaIds": AREA_IDS, "foreTime": "2026-08-16 08:00:00", "beginTime": "2026-08-16 08:00:00", "endTime": "2026-08-17 08:00:00",
       "intval": 24, "modelTypes": ["ECMF"], "range": "9", "isClimateImg": False})

# 【4】area_rainfall/forecast：起报对齐窗口
probe("area_rainfall/forecast（foreTime==beginTime 对齐）",
      f"{BASE}/openapi/area_rainfall/forecast",
      {"areaIds": AREA_IDS, "foreTime": FT, "beginTime": FT, "endTime": FE,
       "intval": 24, "modelTypes": ["ECMF"], "range": "9"})
