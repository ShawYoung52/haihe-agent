"""14所接口聚焦诊断脚本（内网运行，第二轮）。

第一轮结论：API 服务在 8001（basin_drawing image 200、data 为相对路径）；
8080 是共享平台前端页面非 API。本轮聚焦两个未决问题：
1. rainfall_describe 用「实际问答同款参数」是否成功、成功时 data 是什么结构、
   指向的图片 URL 能否拉取（魔数）；
2. basin_drawing areas @8001 用更长超时是否通。

运行：python diagnose_14su.py   （需要 requests）
"""
import requests
from datetime import datetime, timedelta

HOST = "10.226.107.35"
BASE = f"http://{HOST}:8001"


def show_bytes(label: str, resp: requests.Response) -> None:
    print(f"  {label}: HTTP {resp.status_code} ct={resp.headers.get('content-type')}")
    print(f"  first 16 bytes hex: {resp.content[:16].hex()}")
    print(f"  first 16 bytes repr: {resp.content[:16]!r}")


# ---- 1) rainfall_describe：实际问答同款默认窗口（当前整点往前 24h）----
now = datetime.now()
end = now.replace(minute=0, second=0, microsecond=0)
begin = end - timedelta(hours=24)
url = f"{BASE}/openapi/rainfall_describe/real"
for is_climate in (False, True):
    payload = {
        "areaIds": [6, 7, 8, 9, 10, 11, 12, 13, 14],
        "beginTime": begin.strftime("%Y-%m-%d %H:00:00"),
        "endTime": end.strftime("%Y-%m-%d %H:00:00"),
        "interval": 24,
        "range": "9",
        "type": "0",
        "isClimateImg": is_climate,
    }
    print(f"\n===== rainfall_describe isClimateImg={is_climate} =====")
    print("payload:", payload)
    try:
        r = requests.post(url, json=payload, timeout=60)
        print(f"HTTP {r.status_code}")
        obj = r.json()
        print("top keys:", list(obj.keys()) if isinstance(obj, dict) else type(obj).__name__)
        d = obj.get("data") if isinstance(obj, dict) else None
        print("data type:", type(d).__name__)
        if isinstance(d, str):
            print("data head(200):", repr(d[:200]))
            print("data len:", len(d))
            # data 是相对路径/URL → 拼 base 拉取看魔数
            if d.startswith("http"):
                show_bytes("fetch data URL", requests.get(d, timeout=30))
            elif d.startswith("/"):
                show_bytes(f"fetch {BASE}{d}", requests.get(f"{BASE}{d}", timeout=30))
        elif isinstance(d, dict):
            print("data keys:", list(d.keys()))
            for k, v in d.items():
                print(f"  {k}: {type(v).__name__} {repr(str(v)[:120])}")
        elif isinstance(d, list):
            print("data len:", len(d), "| first:", repr(str(d[0])[:200]) if d else "empty")
        else:
            print("data repr:", repr(d)[:300])
    except Exception as e:
        print(f"EXC {type(e).__name__}: {e}")


# ---- 2) basin_drawing areas @8001，60s 超时 ----
print("\n===== basin_drawing areas @8001 (timeout=90) =====")
try:
    r = requests.get(f"{BASE}/openapi/basin_drawing/areas", timeout=90)
    print(f"HTTP {r.status_code} ct={r.headers.get('content-type')}")
    obj = r.json()
    print("top keys:", list(obj.keys()))
    d = obj.get("data")
    if isinstance(d, list):
        print("data len:", len(d))
        if d:
            print("first item keys:", list(d[0].keys()) if isinstance(d[0], dict) else type(d[0]).__name__)
            print("first item:", repr(str(d[0])[:300]))
    else:
        print("data repr:", repr(str(d)[:300]))
except Exception as e:
    print(f"EXC {type(e).__name__}: {e}")


# ---- 3) basin_drawing image 成功后，拉取返回的相对路径验证图片可达 ----
print("\n===== basin_drawing image → 图片 URL 可达性 =====")
img_payload = {
    "sceneType": "REALTIME",
    "productType": "STATION_RAIN",
    "parentAreaId": 1,
    "areaCodes": ["ALL"],
    "beginTime": "2026-08-16 20:00",
    "endTime": "2026-08-17 20:00",
    "mainTitle": "诊断测试",
    "subTitle": "test",
    "showRainValue": True,
    "showAreaName": True,
}
try:
    r = requests.post(f"{BASE}/openapi/basin_drawing/image?forceCreate=1", json=img_payload, timeout=60)
    print(f"image HTTP {r.status_code}")
    d = r.json().get("data")
    print("data:", repr(d[:200]) if isinstance(d, str) else d)
    if isinstance(d, str) and d:
        tgt = d if d.startswith("http") else f"{BASE}{d}"
        try:
            show_bytes("fetch image URL", requests.get(tgt, timeout=30))
        except Exception as e:
            print(f"  fetch image EXC {type(e).__name__}: {e}")
except Exception as e:
    print(f"image EXC {type(e).__name__}: {e}")
