"""14所接口聚焦诊断脚本（第三轮，内网运行）。

目标：
1. 中文字体是否可用（渲染长图的前提）；
2. 组合长图失败的三个板块接口——实况面雨量图 / 预报面雨量图 / 面雨量预报——的真实响应。

运行：python diagnose_14su.py   （需要 requests）
"""
import requests
from datetime import datetime, timedelta

HOST = "10.226.107.35"
BASE = f"http://{HOST}:8001"

print("=" * 60)
print("【0】中文字体检查")
try:
    from custom_tools.composite_longimg_tool import _find_cjk_font
    print("font:", _find_cjk_font())
except Exception as e:
    print(f"import/探测异常: {e}")

now = datetime.now()
end = now.replace(minute=0, second=0, microsecond=0)
begin = end - timedelta(hours=24)
AREA_IDS = [6, 7, 8, 9, 10, 11, 12, 13, 14]
B, E = begin.strftime("%Y-%m-%d %H:00:00"), end.strftime("%Y-%m-%d %H:00:00")


def probe(label, method, url, payload=None, timeout=60):
    print(f"\n===== {label} =====")
    try:
        if method == "POST":
            r = requests.post(url, json=payload, timeout=timeout)
        else:
            r = requests.get(url, params=payload, timeout=timeout)
        print(f"HTTP {r.status_code} ct={r.headers.get('content-type')}")
        text = r.text
        print("body head(300):", repr(text[:300]))
        try:
            obj = r.json()
            if isinstance(obj, dict) and "data" in obj:
                d = obj["data"]
                print("data type:", type(d).__name__)
                if isinstance(d, str):
                    print("data head(150):", repr(d[:150]), "len=", len(d))
                elif isinstance(d, dict):
                    print("data keys:", list(d.keys()))
                elif isinstance(d, list):
                    print("data len:", len(d), "| first:", repr(str(d[0])[:150]) if d else "empty")
        except Exception:
            pass
    except Exception as e:
        print(f"EXC {type(e).__name__}: {e}")


# 【1】实况面雨量图
probe("area_rain_real_img POST",
      "POST", f"{BASE}/openapi/meteor_img/area_rain_real_img?forceCreate=1",
      {"areaIds": AREA_IDS, "beginTime": B, "endTime": E, "interval": 24, "range": "9", "type": "0", "isClimateImg": False})

# 【2】预报面雨量图（foreTime 取最近 08/20 起报）
probe("area_rain_fore_img POST",
      "POST", f"{BASE}/openapi/meteor_img/area_rain_fore_img?forceCreate=1",
      {"areaIds": AREA_IDS, "foreTime": "2026-08-17 08:00:00", "beginTime": B, "endTime": E,
       "intval": 24, "modelTypes": ["ECMF"], "range": "9", "isClimateImg": False})

# 【3】面雨量预报
probe("area_rainfall/forecast POST",
      "POST", f"{BASE}/openapi/area_rainfall/forecast",
      {"areaIds": AREA_IDS, "foreTime": "2026-08-17 08:00:00", "beginTime": B, "endTime": E,
       "intval": 24, "modelTypes": ["ECMF"], "range": "9"})

# 【4】顺带确认：降水实况图（组合长图里成功的那几个）
probe("stationRainRealImg POST（对照组）",
      "POST", f"{BASE}/openapi/meteor_img/stationRainRealImg?forceCreate=1",
      {"areaIds": AREA_IDS, "beginTime": B, "endTime": E, "interval": 24, "range": "9", "type": "0", "isClimateImg": False})
