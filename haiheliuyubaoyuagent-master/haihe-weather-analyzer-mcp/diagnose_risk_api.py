"""诊断脚本：在 MCP 服务器上运行，测试风险预警后端连通性。

格式确认（同事提供）：
  startTime, endTime, fcstTime: YYYYMMDDHHmmss（北京时间）
  fcstTime 取最近一个 08:00 或 20:00 起报时次
  示例：20260720200000, 20260721200000, 20260720200000

用法：在 MCP 服务器上执行
  python diagnose_risk_api.py
"""
import datetime as _dt
import socket
import requests

BASE = "http://10.226.107.35:8070"
PATH = "/hhfw/riskWarnNew/findDataListByConfig"

headers = {
    "Accept": "application/json",
    "User-Agent": "haihe-weather-analyzer/1.0",
}

# Compute expected time values: most recent 08/20 cycle Beijing time
beijing = _dt.timezone(_dt.timedelta(hours=8))
now = _dt.datetime.now(beijing)
if now.hour >= 20:
    fcst_hour = 20
elif now.hour >= 8:
    fcst_hour = 8
else:
    fcst_hour = 20
    now -= _dt.timedelta(days=1)
fcst = now.replace(hour=fcst_hour, minute=0, second=0, microsecond=0)
fcst_str = fcst.strftime("%Y%m%d%H%M%S")
end_str = (fcst + _dt.timedelta(hours=24)).strftime("%Y%m%d%H%M%S")

print(f"Current Beijing time: {_dt.datetime.now(beijing).strftime('%Y%m%d%H%M%S')}")
print(f"Computed fcstTime:    {fcst_str}")
print(f"Computed startTime:   {fcst_str}")
print(f"Computed endTime:     {end_str}")

# Test 1: Full params (should succeed)
print(f"\n=== Test 1: GET with startTime/endTime/fcstTime ===")
try:
    resp = requests.get(
        f"{BASE}{PATH}",
        params={
            "model": "EC",
            "type": 2,
            "startTime": fcst_str,
            "endTime": end_str,
            "fcstTime": fcst_str,
        },
        headers=headers,
        timeout=10,
    )
    print(f"Status: {resp.status_code}")
    print(f"Body:   {resp.text[:500]}")
except Exception as e:
    print(f"ERROR: {e}")

# Test 2: Without time params (still 500?)
print(f"\n=== Test 2: GET without time params (just model+type) ===")
try:
    resp = requests.get(
        f"{BASE}{PATH}",
        params={"model": "EC", "type": 2},
        headers=headers,
        timeout=10,
    )
    print(f"Status: {resp.status_code}")
    print(f"Body:   {resp.text[:300]}")
except Exception as e:
    print(f"ERROR: {e}")

# Test 3: Missing only fcstTime
print(f"\n=== Test 3: GET with startTime+endTime but no fcstTime ===")
try:
    resp = requests.get(
        f"{BASE}{PATH}",
        params={
            "model": "EC", "type": 2,
            "startTime": fcst_str,
            "endTime": end_str,
        },
        headers=headers,
        timeout=10,
    )
    print(f"Status: {resp.status_code}")
    print(f"Body:   {resp.text[:300]}")
except Exception as e:
    print(f"ERROR: {e}")

# Test 4: TCP
print(f"\n=== Test 4: TCP port 8070 ===")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5)
try:
    result = sock.connect_ex(("10.226.107.35", 8070))
    print(f"TCP: {'OK' if result == 0 else f'FAILED (code={result})'}")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    sock.close()
