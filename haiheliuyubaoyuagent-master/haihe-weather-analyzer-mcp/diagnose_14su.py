"""14所接口联调诊断脚本（内网运行）。

用途：确认两个待定事实——
1. `/openapi/rainfall_describe/real`（降水实况文字长图）响应里 data 字段到底是什么结构；
2. `/openapi/basin_drawing/areas` 与 `/openapi/basin_drawing/image`（分区出图）正确端口是 8001 还是 8080。

运行：python diagnose_14su.py   （需要 requests）
"""
import requests

HOST = "10.226.107.35"


def show(label: str, resp: requests.Response) -> None:
    print(f"\n===== {label} =====")
    print(f"HTTP {resp.status_code}")
    body = resp.text
    print(f"Content-Type: {resp.headers.get('content-type')}")
    try:
        obj = resp.json()
        print("JSON keys:", list(obj.keys()) if isinstance(obj, dict) else type(obj).__name__)
        if isinstance(obj, dict) and "data" in obj:
            d = obj["data"]
            print("data type:", type(d).__name__)
            if isinstance(d, str):
                print("data head(120):", repr(d[:120]))
                print("data len:", len(d))
            elif isinstance(d, dict):
                print("data keys:", list(d.keys()))
                for k, v in d.items():
                    print(f"  {k}: {type(v).__name__} head={repr(str(v)[:80])}")
            elif isinstance(d, list):
                print("data len:", len(d), "| first item head:", repr(str(d[0])[:120]) if d else "empty")
            else:
                print("data repr head:", repr(str(d)[:200]))
        else:
            print("body head(400):", repr(body[:400]))
    except Exception:
        print("非 JSON 响应, head(400):", repr(body[:400]))


# 1) rainfall_describe —— 8 个端口都试（确认访问地址）
for port in (8001, 8080):
    url = f"http://{HOST}:{port}/openapi/rainfall_describe/real"
    payload = {
        "areaIds": [6, 7, 8, 9, 10, 11, 12, 13, 14],
        "beginTime": "2026-08-16 08:00:00",
        "endTime": "2026-08-17 08:00:00",
        "interval": 24,
        "range": "9",
        "type": "0",
        "isClimateImg": False,
    }
    try:
        show(f"rainfall_describe POST {url}", requests.post(url, json=payload, timeout=60))
    except Exception as e:
        print(f"\n===== rainfall_describe POST {url} =====\nEXC: {type(e).__name__}: {e}")

# 2) basin_drawing areas —— 8001 与 8080 对比
for port in (8001, 8080):
    url = f"http://{HOST}:{port}/openapi/basin_drawing/areas"
    try:
        show(f"basin_drawing areas GET {url}", requests.get(url, timeout=30))
    except Exception as e:
        print(f"\n===== basin_drawing areas GET {url} =====\nEXC: {type(e).__name__}: {e}")

# 3) basin_drawing image —— 8001 与 8080 对比
for port in (8001, 8080):
    url = f"http://{HOST}:{port}/openapi/basin_drawing/image?forceCreate=1"
    payload = {
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
        show(f"basin_drawing image POST {url}", requests.post(url, json=payload, timeout=60))
    except Exception as e:
        print(f"\n===== basin_drawing image POST {url} =====\nEXC: {type(e).__name__}: {e}")
