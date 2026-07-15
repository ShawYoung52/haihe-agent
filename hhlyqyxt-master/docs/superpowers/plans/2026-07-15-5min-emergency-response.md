# 5 分钟降雨应急响应监测实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在牵引智能体侧新增 5 分钟应急响应监测模块，按国家站 12h/24h 暴雨/大暴雨/特大暴雨占比落库，并提供查询接口。

**Architecture:** 新增独立模块 `ScheduledTask/emergency_response_monitor.py` 负责读取 5 分钟 CSV、过滤国家站、计算分级占比并入库；`Models/QyEmergencyResponseMonitor.py` 提供 ORM；`Controller/tool_router.py` 提供只读接口；在 `stationProcessMin.py` 现有 `calcmaxdataseg5min()` 末尾调用新模块。

**Tech Stack:** Python, pandas, SQLAlchemy 1.4/2.0 ORM, PostgreSQL, FastAPI, pytest.

---

## 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `Models/QyEmergencyResponseMonitor.py` | 创建 | ORM 模型 |
| `ScheduledTask/emergency_response_monitor.py` | 创建 | 核心计算与入库逻辑 |
| `ScheduledTask/stationProcessMin.py` | 修改 | 保留 `Station_levl` 并在主流程末尾调用应急响应模块 |
| `Controller/tool_router.py` | 修改 | 新增 `/emergency-response/latest` 接口 |
| `utils/tests/test_emergency_response_monitor.py` | 创建 | 单元测试 |

---

## Task 1：创建 ORM 模型

**Files:**
- Create: `Models/QyEmergencyResponseMonitor.py`

### Step 1.1：编写模型文件

```python
from datetime import datetime

from pydantic import ConfigDict
from sqlalchemy import Column, DateTime, Integer, Numeric, SmallInteger
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class QyEmergencyResponseMonitor(Base):
    __tablename__ = "qy_emergency_response_monitor"

    id = Column(Integer, primary_key=True)
    datatime = Column(DateTime, nullable=False)
    minute_monitor_id = Column(Integer)
    total_national_stations = Column(Integer, nullable=False, default=0)
    station_12h_baoyu = Column(Integer, nullable=False, default=0)
    ratio_12h_baoyu = Column(Numeric(6, 4), nullable=False, default=0)
    station_24h_baoyu = Column(Integer, nullable=False, default=0)
    ratio_24h_baoyu = Column(Numeric(6, 4), nullable=False, default=0)
    station_24h_dabaoyu = Column(Integer, nullable=False, default=0)
    ratio_24h_dabaoyu = Column(Numeric(6, 4), nullable=False, default=0)
    station_24h_tedabaoyu = Column(Integer, nullable=False, default=0)
    ratio_24h_tedabaoyu = Column(Numeric(6, 4), nullable=False, default=0)
    response_level = Column(SmallInteger, nullable=False, default=0)
    create_time = Column(DateTime, nullable=False, default=datetime.now)

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self) -> str:
        return (
            f"<QyEmergencyResponseMonitor id={self.id} "
            f"datatime={self.datatime} level={self.response_level}>"
        )
```

### Step 1.2：建表 SQL（在内网离线服务器执行）

```sql
CREATE TABLE IF NOT EXISTS qy_emergency_response_monitor (
    id SERIAL PRIMARY KEY,
    datatime TIMESTAMP NOT NULL,
    minute_monitor_id INTEGER,
    total_national_stations INTEGER NOT NULL DEFAULT 0,
    station_12h_baoyu INTEGER NOT NULL DEFAULT 0,
    ratio_12h_baoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_baoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_baoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_dabaoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_dabaoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_tedabaoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_tedabaoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    response_level SMALLINT NOT NULL DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_qy_emergency_response_monitor_datatime
    ON qy_emergency_response_monitor(datatime DESC);
```

### Step 1.3：提交

```bash
git add Models/QyEmergencyResponseMonitor.py
git commit -m "feat: add QyEmergencyResponseMonitor ORM model"
```

---

## Task 2：核心计算模块（TDD）

**Files:**
- Create: `ScheduledTask/emergency_response_monitor.py`
- Create: `utils/tests/test_emergency_response_monitor.py`

### Step 2.1：写失败测试

```python
import pandas as pd
import pytest

from ScheduledTask.emergency_response_monitor import (
    compute_emergency_response_stats,
    filter_national_stations,
    load_minute_rainfall,
    resolve_response_level,
)


def _make_df() -> pd.DataFrame:
    """构造 24h 内 10 个国家站的 5 分钟数据。"""
    records = []
    base_time = pd.Timestamp("2026-07-15 12:00:00")
    # 10 个国家站
    for i in range(10):
        records.append({
            "Station_Id_C": f"A{i:02d}",
            "Datetime": base_time,
            "PRE": 60.0,   # 暴雨
            "Lat": 39.0,
            "Lon": 117.0,
            "City": "天津市",
            "Station_Name": f"站点{i}",
            "Cnty": "区{i}",
            "Province": "天津市",
            "Town": "镇{i}",
            "Station_levl": "011",
        })
    return pd.DataFrame(records)


def test_filter_national_stations():
    df = _make_df()
    df = pd.concat([df, pd.DataFrame([{
        "Station_Id_C": "X01",
        "Datetime": df["Datetime"].iloc[0],
        "PRE": 100.0,
        "Lat": 39.0, "Lon": 117.0,
        "City": "", "Station_Name": "非国家站",
        "Cnty": "", "Province": "", "Town": "",
        "Station_levl": "999",
    }])], ignore_index=True)

    national = filter_national_stations(df)
    assert len(national) == 10
    assert "X01" not in national["Station_Id_C"].values


def test_resolve_response_level_iv():
    stats = {
        "ratio_24h_baoyu": 0.20,
        "ratio_12h_baoyu": 0.0,
        "ratio_24h_dabaoyu": 0.0,
        "ratio_24h_tedabaoyu": 0.0,
    }
    assert resolve_response_level(stats) == 4


def test_resolve_response_level_i():
    stats = {
        "ratio_24h_baoyu": 0.20,
        "ratio_12h_baoyu": 0.20,
        "ratio_24h_dabaoyu": 0.15,
        "ratio_24h_tedabaoyu": 0.15,
    }
    assert resolve_response_level(stats) == 1


def test_compute_emergency_response_stats():
    df = _make_df()
    end_time = pd.Timestamp("2026-07-15 12:00:00")
    stats = compute_emergency_response_stats(df, end_time)
    assert stats["total_national_stations"] == 10
    assert stats["station_24h_baoyu"] == 10
    assert stats["ratio_24h_baoyu"] == 1.0
    assert stats["station_12h_baoyu"] == 10


def test_load_minute_rainfall_pads_station_level(tmp_path):
    csv = tmp_path / "rain.csv"
    df = _make_df()
    df["Station_levl"] = "11"  # 模拟缺失前导零
    df.to_csv(csv, index=False)
    loaded = load_minute_rainfall(str(csv))
    assert set(loaded["Station_levl"].unique()) == {"011"}
```

Run:

```bash
python -m pytest utils/tests/test_emergency_response_monitor.py -v
```

Expected: failures because target functions do not exist.

### Step 2.2：实现核心模块

```python
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session as DBSession

logger = logging.getLogger(__name__)

NATIONAL_STATION_LEVELS = {"011", "012", "013", "016"}

BAOYU_LOW = 50.0
DABAOYU_LOW = 100.0
TEDABAOYU_LOW = 250.0


def load_minute_rainfall(csv_path: str) -> pd.DataFrame:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Minute rainfall CSV not found: {csv_path}")

    df = pd.read_csv(csv_path, dtype={"Station_levl": str})
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df["PRE"] = pd.to_numeric(df["PRE"], errors="coerce").fillna(0)
    df["Station_levl"] = (
        df["Station_Id_C"]
        .astype(str)
        .str.strip()
        .str.zfill(3)
    )
    return df


def filter_national_stations(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["Station_levl"].isin(NATIONAL_STATION_LEVELS)].copy()


def _count_in_range(station_sum: pd.Series, low: float, high: float) -> int:
    if high == float("inf"):
        return int((station_sum >= low).sum())
    return int(((station_sum >= low) & (station_sum < high)).sum())


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def compute_window_stats(
    df: pd.DataFrame, end_time: datetime, window_hours: int
) -> dict:
    start_time = end_time - pd.Timedelta(hours=window_hours)
    window_df = df[(df["Datetime"] > start_time) & (df["Datetime"] <= end_time)].copy()

    if window_df.empty:
        return {
            "total": 0,
            "baoyu": 0,
            "dabaoyu": 0,
            "tedabaoyu": 0,
            "ratio_baoyu": 0.0,
            "ratio_dabaoyu": 0.0,
            "ratio_tedabaoyu": 0.0,
        }

    station_sum = (
        window_df.groupby("Station_Id_C", as_index=False, sort=False)["PRE"]
        .sum()
    )
    total = station_sum["Station_Id_C"].nunique()

    baoyu = _count_in_range(station_sum["PRE"], BAOYU_LOW, DABAOYU_LOW)
    dabaoyu = _count_in_range(station_sum["PRE"], DABAOYU_LOW, TEDABAOYU_LOW)
    tedabaoyu = _count_in_range(station_sum["PRE"], TEDABAOYU_LOW, float("inf"))

    return {
        "total": total,
        "baoyu": baoyu,
        "dabaoyu": dabaoyu,
        "tedabaoyu": tedabaoyu,
        "ratio_baoyu": _ratio(baoyu, total),
        "ratio_dabaoyu": _ratio(dabaoyu, total),
        "ratio_tedabaoyu": _ratio(tedabaoyu, total),
    }


def compute_emergency_response_stats(
    df: pd.DataFrame, end_time: datetime
) -> dict:
    national_df = filter_national_stations(df)
    stats_12h = compute_window_stats(national_df, end_time, 12)
    stats_24h = compute_window_stats(national_df, end_time, 24)

    return {
        "datatime": end_time,
        "total_national_stations": stats_24h["total"],
        "station_12h_baoyu": stats_12h["baoyu"],
        "ratio_12h_baoyu": stats_12h["ratio_baoyu"],
        "station_24h_baoyu": stats_24h["baoyu"],
        "ratio_24h_baoyu": stats_24h["ratio_baoyu"],
        "station_24h_dabaoyu": stats_24h["dabaoyu"],
        "ratio_24h_dabaoyu": stats_24h["ratio_dabaoyu"],
        "station_24h_tedabaoyu": stats_24h["tedabaoyu"],
        "ratio_24h_tedabaoyu": stats_24h["ratio_tedabaoyu"],
    }


def resolve_response_level(stats: dict) -> int:
    if stats["ratio_24h_tedabaoyu"] >= 0.15:
        return 1
    if stats["ratio_24h_dabaoyu"] >= 0.15:
        return 2
    if stats["ratio_12h_baoyu"] >= 0.20:
        return 3
    if stats["ratio_24h_baoyu"] >= 0.20:
        return 4
    return 0


def save_emergency_response_monitor(
    session: Session,
    stats: dict,
    datatime: datetime,
    minute_monitor_id: Optional[int] = None,
) -> QyEmergencyResponseMonitor:
    record = QyEmergencyResponseMonitor(
        datatime=datatime,
        minute_monitor_id=minute_monitor_id,
        total_national_stations=stats["total_national_stations"],
        station_12h_baoyu=stats["station_12h_baoyu"],
        ratio_12h_baoyu=stats["ratio_12h_baoyu"],
        station_24h_baoyu=stats["station_24h_baoyu"],
        ratio_24h_baoyu=stats["ratio_24h_baoyu"],
        station_24h_dabaoyu=stats["station_24h_dabaoyu"],
        ratio_24h_dabaoyu=stats["ratio_24h_dabaoyu"],
        station_24h_tedabaoyu=stats["station_24h_tedabaoyu"],
        ratio_24h_tedabaoyu=stats["ratio_24h_tedabaoyu"],
        response_level=resolve_response_level(stats),
    )
    session.add(record)
    session.commit()
    return record


def run_emergency_response_monitor(
    csv_path: str,
    datatime: Optional[datetime] = None,
    minute_monitor_id: Optional[int] = None,
) -> Optional[QyEmergencyResponseMonitor]:
    try:
        df = load_minute_rainfall(csv_path)
    except FileNotFoundError:
        logger.warning("CSV not found, skip emergency response monitor: %s", csv_path)
        return None

    if datatime is None:
        datatime = df["Datetime"].max()

    stats = compute_emergency_response_stats(df, datatime)

    session = DBSession()
    try:
        record = save_emergency_response_monitor(
            session, stats, datatime, minute_monitor_id
        )
        return record
    except Exception:
        session.rollback()
        logger.exception("Failed to save emergency response monitor record")
        raise
    finally:
        session.close()
```

### Step 2.3：运行测试

```bash
python -m pytest utils/tests/test_emergency_response_monitor.py -v
```

Expected: PASS.

### Step 2.4：提交

```bash
git add ScheduledTask/emergency_response_monitor.py utils/tests/test_emergency_response_monitor.py
git commit -m "feat: add 5-minute emergency response monitor calculation and tests"
```

---

## Task 3：在 stationProcessMin.py 中保留 Station_levl 并接入新模块

**Files:**
- Modify: `ScheduledTask/stationProcessMin.py`

### Step 3.1：在 `unionmindataby10minuteto24h` 和 `circleadd5min` 的 resample agg 中加入 `Station_levl`

两处 agg dict 都增加：

```python
"Station_levl": "first",
```

### Step 3.2：在 `calcmaxdataseg5min` 末尾调用应急响应模块

在 `session.close()` 之前、主记录已 commit 之后：

```python
from ScheduledTask.emergency_response_monitor import run_emergency_response_monitor

run_emergency_response_monitor(
    csv_path=tempfile,
    datatime=end_time,
    minute_monitor_id=qmm.id,
)
```

### Step 3.3：运行调度脚本 Dry Run

```bash
python -m ScheduledTask.stationProcessMin
# 因主函数会启动 scheduler，dry run 可注释 main() 调用后执行一次 calcmaxdataseg5min
```

### Step 3.4：提交

```bash
git add ScheduledTask/stationProcessMin.py
git commit -m "feat: integrate emergency response monitor into 5-minute scheduler"
```

---

## Task 4：新增查询接口

**Files:**
- Modify: `Controller/tool_router.py`

### Step 4.1：新增接口

```python
from sqlalchemy import desc
from sqlalchemy.orm import Session as DBSession

from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session


@toolrouter.get("/emergency-response/latest")
def get_latest_emergency_response(limit: int = 1):
    if limit < 1 or limit > 100:
        limit = 1
    session = Session()
    try:
        rows = (
            session.query(QyEmergencyResponseMonitor)
            .order_by(desc(QyEmergencyResponseMonitor.datatime))
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "datatime": r.datatime.strftime("%Y-%m-%d %H:%M:%S") if r.datatime else None,
                "minute_monitor_id": r.minute_monitor_id,
                "total_national_stations": r.total_national_stations,
                "station_12h_baoyu": r.station_12h_baoyu,
                "ratio_12h_baoyu": float(r.ratio_12h_baoyu),
                "station_24h_baoyu": r.station_24h_baoyu,
                "ratio_24h_baoyu": float(r.ratio_24h_baoyu),
                "station_24h_dabaoyu": r.station_24h_dabaoyu,
                "ratio_24h_dabaoyu": float(r.ratio_24h_dabaoyu),
                "station_24h_tedabaoyu": r.station_24h_tedabaoyu,
                "ratio_24h_tedabaoyu": float(r.ratio_24h_tedabaoyu),
                "response_level": r.response_level,
                "create_time": r.create_time.strftime("%Y-%m-%d %H:%M:%S") if r.create_time else None,
            }
            for r in rows
        ]
    finally:
        session.close()
```

### Step 4.2：本地启动 FastAPI 并验证接口

```bash
python main.py
# 浏览器/Postman 访问：
# http://localhost:7000/tool/emergency-response/latest?limit=5
```

### Step 4.3：提交

```bash
git add Controller/tool_router.py
git commit -m "feat: add emergency response latest query endpoint"
```

---

## Task 5：代码审查

### Step 5.1：使用 code-review 技能

调用 `code-review` 或 `superpowers:receiving-code-review` 对当前 diff 进行审查，重点检查：
- 数据库 session 是否正确关闭；
- 空数据边界；
- 重复代码；
- 类型一致性。

### Step 5.2：修复审查意见

逐条处理，更新代码和测试。

### Step 5.3：提交

```bash
git commit -am "refactor: address code review feedback"
```

---

## Task 6：简化重构（code-simplifier）

### Step 6.1：调用 code-simplifier agent

重点简化 `ScheduledTask/emergency_response_monitor.py`，检查是否可：
- 合并重复的分级统计逻辑；
- 提取常量；
- 减少不必要的 copy/reset_index。

### Step 6.2：运行测试确保行为不变

```bash
python -m pytest utils/tests/test_emergency_response_monitor.py -v
```

### Step 6.3：提交

```bash
git commit -am "refactor: simplify emergency response monitor implementation"
```

---

## Task 7：验证与端到端测试

### Step 7.1：运行全部测试

```bash
python -m pytest utils/tests/ -v
```

### Step 7.2：手动触发一次主流程

在有真实 CSV 和数据库连接的环境中：

```bash
python -c "from ScheduledTask.stationProcessMin import calcmaxdataseg5min; calcmaxdataseg5min()"
```

检查 `qy_emergency_response_monitor` 是否有新记录。

### Step 7.3：提交

```bash
git commit -am "test: verify emergency response monitor end-to-end"
```

---

## Task 8：文档与记忆

### Step 8.1：更新 `findings.md` 与 `progress.md`

记录实现细节、测试结论、表结构。

### Step 8.2：更新 CLAUDE.md（如项目有）

在根目录 CLAUDE.md 中补充：
- 5 分钟应急响应监测模块位置；
- 表名与触发条件；
- 接口地址。

### Step 8.3：写入 claude-mem 记忆

保存项目记忆：海河流域应急响应监测表为 `qy_emergency_response_monitor`，国家站级别代码 `"011/012/013/016"`，牵引智能体只输出实况指标，问答智能体负责持续/启动判断。

### Step 8.4：最终提交

```bash
git add -A
git commit -m "docs: update planning docs and project memory for emergency response monitor"
```

---

## Spec 覆盖自检

| 设计文档要求 | 对应任务 |
|--------------|----------|
| 国家站过滤 `"011/012/013/016"` | Task 2 |
| 12h/24h 分级统计 | Task 2 |
| 表 `qy_emergency_response_monitor` | Task 1 |
| 接入 5 分钟调度 | Task 3 |
| 只读查询接口 | Task 4 |
| 异常处理与空数据 | Task 2、Task 5 |
| 单元测试 | Task 2、Task 7 |
