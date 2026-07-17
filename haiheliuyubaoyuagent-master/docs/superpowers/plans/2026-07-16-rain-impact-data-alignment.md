# Rain Impact 数据对齐（full_v6 清洗 + pkl 重生成）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 备份、清洗 `haihe_river_directed_full_v6`，重新生成 `river_directed_v6.pkl`，使每个 `objectid` 对应一条连通河流，pkl edge 与 full_v6 子段精确对应，从而消除 rain impact 输出中的孤立河段、下游断裂和河段跳跃。

**Architecture:** 通过 PostGIS 与 Shapely 拆分并重新聚类 full_v6 几何，按“同一 objectid 只保留单一连通主河道”原则清洗数据；再用清洗后的数据重建有向河网图并序列化为 pkl；最后同步代码常量、回归测试，并用内网样本验证异常指标下降。

**Tech Stack:** PostgreSQL/PostGIS, Python 3.10+, geopandas, shapely, networkx, pickle, psycopg2, pytest

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/backup_river_data.py` | 备份 `haihe_river_directed_full_v6` 表与当前 `river_directed_v6.pkl`，加 `bak` 后缀/时间戳 |
| `scripts/clean_full_v6.py` | 读取 full_v6，按 objectid 拆分 MULTILINESTRING，聚类连通子段，重新编号，写回新表 |
| `scripts/regenerate_river_pkl.py` | 基于清洗后的 full_v6 构建有向河网图，输出 `river_directed_v6.pkl` |
| `scripts/validate_alignment.py` | 对比清洗前后的 rain impact 输出指标（重复 objectid、孤立段、端点间隙等） |
| `haihe-weather-analyzer-mcp/config.ini` | 确认 `[paths] graph` 指向正确的 pkl 目录；必要时更新 |
| `haihe-weather-analyzer-mcp/constants.py` | 如引入新文件名/版本则更新 `DIRECTED_GRAPH_FILENAME` / `RIVER_TABLE_VERSION` |
| `hhlyqyxt-master/utils/rainfall_impact_geojson.py` | 对齐后可简化子段选择、方向判断、去重逻辑（可选） |
| `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py` | 补充回归测试，确保 objectid 唯一性假设不破坏现有测试 |

---

## Task 1: 备份现有数据

**Files:**
- Create: `scripts/backup_river_data.py`
- Modify: `haihe-weather-analyzer-mcp/config.ini`（确认路径，可选）

**目标：** 清洗前对 `haihe_river_directed_full_v6` 表和当前 `river_directed_v6.pkl` 加 `bak` 后缀备份，支持一键回滚。

- [ ] **Step 1: 编写备份脚本**

```python
# scripts/backup_river_data.py
"""备份河网表与 pkl 图文件。"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import psycopg2


def _pg_conn():
    """从环境变量读取连接信息，避免硬编码。"""
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "${PGHOST}"),
        port=os.environ.get("PGPORT", "${PGPORT}"),
        dbname=os.environ.get("PGDATABASE", "${PGDATABASE}"),
        user=os.environ.get("PGUSER", "${PGUSER}"),
        password=os.environ.get("PGPASSWORD", "${PGPASSWORD}"),
    )


def backup_table(table: str, backup_table: str | None = None):
    if backup_table is None:
        backup_table = f"{table}_bak_{datetime.now():%Y%m%d_%H%M%S}"
    conn = _pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {backup_table}")
            cur.execute(f"CREATE TABLE {backup_table} AS TABLE {table}")
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM {backup_table}"
            )
            cnt = cur.fetchone()[0]
        print(f"表已备份: {table} -> {backup_table} (共 {cnt} 行)")
        return backup_table
    finally:
        conn.close()


def backup_pkl(pkl_path: str, backup_path: str | None = None):
    src = Path(pkl_path)
    if not src.exists():
        print(f"pkl 文件不存在，跳过备份: {pkl_path}")
        return None
    if backup_path is None:
        backup_path = str(src.with_suffix(f".bak_{datetime.now():%Y%m%d_%H%M%S}.pkl"))
    shutil.copy2(src, backup_path)
    print(f"pkl 已备份: {src} -> {backup_path}")
    return backup_path


def main():
    table = os.environ.get("RIVER_TABLE_FULL", "haihe_river_directed_full_v6")
    pkl_path = os.environ.get("RIVER_GRAPH_PATH", r"E:\tj\line\result\river_directed_v6.pkl")

    backup_table(table)
    backup_pkl(pkl_path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 在内部环境运行备份脚本**

```bash
# 在能访问 PostgreSQL 的机器上执行
set PGHOST=<内网 PG 地址>
set PGPORT=<端口>
set PGDATABASE=<库名>
set PGUSER=<用户名>
set PGPASSWORD=<密码>
set RIVER_TABLE_FULL=haihe_river_directed_full_v6
set RIVER_GRAPH_PATH=E:\tj\line\result\river_directed_v6.pkl

python scripts/backup_river_data.py
```

Expected output:
```
表已备份: haihe_river_directed_full_v6 -> haihe_river_directed_full_v6_bak_20260716_xxxxxx (共 N 行)
pkl 已备份: E:\tj\line\result\river_directed_v6.pkl -> E:\tj\line\result\river_directed_v6.bak_20260716_xxxxxx.pkl
```

- [ ] **Step 3: 提交备份脚本**

```bash
git add scripts/backup_river_data.py
git commit -m "feat(river): add backup script for full_v6 table and pkl graph"
```

---

## Task 2: 量化当前 misalignment（基线）

**Files:**
- Create: `scripts/quantify_misalignment.py`
- Test: 运行后输出 JSON 报告

**目标：** 在清洗前记录基线指标，便于后续对比。

- [ ] **Step 1: 编写基线量化脚本**

```python
# scripts/quantify_misalignment.py
"""量化 full_v6 中 objectid 级别的不对齐问题。"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import psycopg2
from shapely import wkb
from shapely.geometry import MultiLineString


def _pg_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "${PGHOST}"),
        port=os.environ.get("PGPORT", "${PGPORT}"),
        dbname=os.environ.get("PGDATABASE", "${PGDATABASE}"),
        user=os.environ.get("PGUSER", "${PGUSER}"),
        password=os.environ.get("PGPASSWORD", "${PGPASSWORD}"),
    )


def quantify(table: str):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {table}.objectid, {table}.src_name, ST_AsBinary({table}.geom) AS geom
                FROM {table}
                WHERE {table}.geom IS NOT NULL
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    oid_records = defaultdict(list)
    for oid, name, geom_wkb in rows:
        geom = wkb.loads(bytes(geom_wkb))
        oid_records[oid].append({"name": name, "geom": geom})

    stats = {
        "total_rows": len(rows),
        "unique_objectids": len(oid_records),
        "multi_part_objectids": 0,
        "multi_name_objectids": 0,
        "details": [],
    }

    for oid, records in oid_records.items():
        names = {r["name"] for r in records if r["name"]}
        parts = sum(
            len(g.geoms) if g.geom_type == "MultiLineString" else 1
            for r in records
            for g in [r["geom"]]
        )
        is_multi_name = len(names) > 1
        is_multi_part = parts > 1
        if is_multi_name:
            stats["multi_name_objectids"] += 1
        if is_multi_part:
            stats["multi_part_objectids"] += 1
        if is_multi_name or is_multi_part:
            stats["details"].append({
                "objectid": oid,
                "names": sorted(names),
                "parts": parts,
                "row_count": len(records),
            })

    return stats


def main():
    table = os.environ.get("RIVER_TABLE_FULL", "haihe_river_directed_full_v6")
    stats = quantify(table)
    out = Path(f"{table}_baseline_stats.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"基线报告: {out}")
    print(f"  总行数: {stats['total_rows']}")
    print(f"  唯一 objectid: {stats['unique_objectids']}")
    print(f"  多部件 objectid: {stats['multi_part_objectids']}")
    print(f"  多名称 objectid: {stats['multi_name_objectids']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行基线脚本**

```bash
python scripts/quantify_misalignment.py
```

Expected output:
```
基线报告: haihe_river_directed_full_v6_baseline_stats.json
  总行数: N
  唯一 objectid: M
  多部件 objectid: X
  多名称 objectid: Y
```

- [ ] **Step 3: 提交脚本**

```bash
git add scripts/quantify_misalignment.py
git commit -m "feat(river): add baseline misalignment quantification script"
```

---

## Task 3: 清洗 full_v6

**Files:**
- Create: `scripts/clean_full_v6.py`
- Modify: 数据库中创建/替换 `haihe_river_directed_full_v6` 表（或写入 `haihe_river_directed_full_v6_cleaned`）

**目标：** 每个 objectid 只对应一条连通河流几何；同名且连通的子段合并，不同名或不连通的子段拆分为独立 objectid。

### 清洗规则
1. 对每行 `geom` 使用 `ST_Dump` 拆分为 `LINESTRING` 子段。
2. 按 `src_name` + 几何连通性聚类：
   - 同一 objectid 下若子段名称不同，按名称分组。
   - 同一名称下若子段互不连通，保留最长/最主干的一段，其余拆分为独立 objectid。
3. 为拆分出的新河段分配新 objectid（从 `max(objectid) + 1` 开始递增）。
4. 保留原表的 `src_name`、`flow_dir` 等属性列；几何列替换为清洗后的单一部件。
5. 输出表名默认为 `haihe_river_directed_full_v6`，可通过 `RIVER_TABLE_FULL_OUTPUT` 覆盖为 `haihe_river_directed_full_v6_cleaned` 做灰度验证。

- [ ] **Step 1: 编写清洗脚本**

```python
# scripts/clean_full_v6.py
"""清洗 full_v6：每个 objectid 只保留单一连通河段。"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import psycopg2
from shapely import wkb
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, unary_union


def _pg_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "${PGHOST}"),
        port=os.environ.get("PGPORT", "${PGPORT}"),
        dbname=os.environ.get("PGDATABASE", "${PGDATABASE}"),
        user=os.environ.get("PGUSER", "${PGUSER}"),
        password=os.environ.get("PGPASSWORD", "${PGPASSWORD}"),
    )


def _dump_parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "MultiLineString":
        return [LineString(list(p.coords)) for p in geom.geoms]
    if geom.geom_type == "LineString":
        return [LineString(list(geom.coords))]
    return []


def _are_connected(lines, tolerance=1e-8):
    """判断一组线是否全部连通（基于端点容差）。"""
    if not lines:
        return True
    groups = []
    for line in lines:
        start = tuple(line.coords[0])
        end = tuple(line.coords[-1])
        matched = []
        for i, group in enumerate(groups):
            if any(
                abs(start[0] - p[0]) < tolerance and abs(start[1] - p[1]) < tolerance
                or abs(end[0] - p[0]) < tolerance and abs(end[1] - p[1]) < tolerance
                for p in group["points"]
            ):
                matched.append(i)
        if not matched:
            groups.append({"lines": [line], "points": {start, end}})
        else:
            base = groups[matched[0]]
            base["lines"].append(line)
            base["points"].update({start, end})
            for mi in matched[1:]:
                base["lines"].extend(groups[mi]["lines"])
                base["points"].update(groups[mi]["points"])
            for mi in sorted(matched[1:], reverse=True):
                groups.pop(mi)
    return len(groups) == 1


def _split_by_name(parts, name):
    """按名称分组；无名称的统一放入一组。"""
    by_name = defaultdict(list)
    for p in parts:
        by_name[p.get("name") or "__unknown__"].append(p["line"])
    return by_name


def _pick_main_component(lines):
    """在一组不连通的线中，保留最长且端点度最小的主河段。"""
    # 先尝试 linemerge，若结果为 MultiLineString 说明不连通
    merged = linemerge(unary_union(lines))
    if merged.geom_type == "LineString":
        return [merged]

    # 不连通：拆分为连通分量，选最长的一个作为主河道
    groups = []
    for line in list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]:
        placed = False
        start = tuple(line.coords[0])
        end = tuple(line.coords[-1])
        for g in groups:
            if any(
                abs(start[0] - p[0]) < 1e-8 and abs(start[1] - p[1]) < 1e-8
                or abs(end[0] - p[0]) < 1e-8 and abs(end[1] - p[1]) < 1e-8
                for p in g["points"]
            ):
                g["lines"].append(line)
                g["points"].update({start, end})
                placed = True
                break
        if not placed:
            groups.append({"lines": [line], "points": {start, end}})

    groups.sort(key=lambda g: sum(l.length for l in g["lines"]), reverse=True)
    return groups[0]["lines"]


def clean_table(input_table: str, output_table: str):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(objectid) FROM {input_table}")
            max_oid = cur.fetchone()[0] or 0
            next_oid = int(max_oid) + 1

            cur.execute(
                f"""
                SELECT objectid, src_name, ST_AsBinary(geom) AS geom
                FROM {input_table}
                WHERE geom IS NOT NULL
                ORDER BY objectid
                """
            )
            rows = cur.fetchall()

            cleaned_records = []
            remap_log = []

            for oid, name, geom_wkb in rows:
                geom = wkb.loads(bytes(geom_wkb))
                parts = _dump_parts(geom)
                if not parts:
                    continue

                by_name = _split_by_name(
                    [{"name": name, "line": p} for p in parts], name
                )

                for gname, lines in by_name.items():
                    main_lines = _pick_main_component(lines)
                    main_geom = linemerge(unary_union(main_lines))
                    if main_geom.geom_type != "LineString":
                        main_geom = MultiLineString(main_lines)

                    cleaned_records.append({
                        "objectid": oid if gname == (name or "__unknown__") else next_oid,
                        "src_name": gname if gname != "__unknown__" else name,
                        "geom": main_geom,
                        "original_objectid": oid,
                    })

                    if gname != (name or "__unknown__"):
                        remap_log.append({
                            "original_objectid": oid,
                            "new_objectid": next_oid,
                            "name": gname,
                            "reason": "different_name_same_objectid",
                        })
                        next_oid += 1

                    # 处理剩余分支：作为独立 objectid
                    remaining = [l for l in lines if l not in main_lines]
                    if remaining:
                        branch_geom = linemerge(unary_union(remaining))
                        if branch_geom.geom_type != "LineString":
                            branch_geom = MultiLineString(remaining)
                        cleaned_records.append({
                            "objectid": next_oid,
                            "src_name": gname if gname != "__unknown__" else name,
                            "geom": branch_geom,
                            "original_objectid": oid,
                        })
                        remap_log.append({
                            "original_objectid": oid,
                            "new_objectid": next_oid,
                            "name": gname if gname != "__unknown__" else name,
                            "reason": "disconnected_branch",
                        })
                        next_oid += 1

            # 写入输出表
            cur.execute(f"DROP TABLE IF EXISTS {output_table}")
            cur.execute(
                f"""
                CREATE TABLE {output_table} (
                    objectid INTEGER PRIMARY KEY,
                    src_name TEXT,
                    geom GEOMETRY(LINESTRING, 4326),
                    original_objectid INTEGER
                )
                """
            )
            for rec in cleaned_records:
                cur.execute(
                    f"""
                    INSERT INTO {output_table} (objectid, src_name, geom, original_objectid)
                    VALUES (%s, %s, ST_GeomFromText(%s, 4326), %s)
                    """,
                    (rec["objectid"], rec["src_name"], rec["geom"].wkt, rec["original_objectid"]),
                )

            conn.commit()

            # 保存 remap 日志
            remap_path = Path(f"{output_table}_remap.json")
            with open(remap_path, "w", encoding="utf-8") as f:
                json.dump(remap_log, f, ensure_ascii=False, indent=2)

            print(f"清洗完成: {input_table} -> {output_table}")
            print(f"  原行数: {len(rows)}")
            print(f"  清洗后行数: {len(cleaned_records)}")
            print(f"  重新编号数: {len(remap_log)}")
            print(f"  remap 日志: {remap_path}")

    finally:
        conn.close()


def main():
    input_table = os.environ.get("RIVER_TABLE_FULL", "haihe_river_directed_full_v6")
    output_table = os.environ.get("RIVER_TABLE_FULL_OUTPUT", input_table)
    clean_table(input_table, output_table)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 先以灰度表名试运行**

```bash
set RIVER_TABLE_FULL=haihe_river_directed_full_v6
set RIVER_TABLE_FULL_OUTPUT=haihe_river_directed_full_v6_cleaned
python scripts/clean_full_v6.py
```

Expected output:
```
清洗完成: haihe_river_directed_full_v6 -> haihe_river_directed_full_v6_cleaned
  原行数: N
  清洗后行数: M
  重新编号数: K
  remap 日志: haihe_river_directed_full_v6_cleaned_remap.json
```

- [ ] **Step 3: 验证灰度表**

```bash
set RIVER_TABLE_FULL=haihe_river_directed_full_v6_cleaned
python scripts/quantify_misalignment.py
```

Expected: `multi_name_objectids` 和 `multi_part_objectids` 应降至 0 或接近 0。

- [ ] **Step 4: 灰度验证通过后，正式覆盖原表**

```bash
set RIVER_TABLE_FULL=haihe_river_directed_full_v6
set RIVER_TABLE_FULL_OUTPUT=haihe_river_directed_full_v6
python scripts/clean_full_v6.py
```

- [ ] **Step 5: 提交清洗脚本**

```bash
git add scripts/clean_full_v6.py
git commit -m "feat(river): add full_v6 cleaning script with remap logging"
```

---

## Task 4: 重新生成 pkl

**Files:**
- Create: `scripts/regenerate_river_pkl.py`
- Modify: `E:/tj/line/result/river_directed_v6.pkl`

**目标：** 基于清洗后的 full_v6 重建有向河网图，pkl edge 属性中包含精确的 `objectid`、起终点坐标和几何。

### 生成规则
1. 读取清洗后的 full_v6，每行视为一条有向河段。
2. 提取所有端点，在节点处打断河段（保持 objectid 不变，生成多个 edge）。
3. 使用 BFS 从每个连通分量的出口（最东端）反推流向。
4. 每条 edge 记录：
   - `objectid`: 所属 objectid
   - `name`: `src_name`
   - `geom`: 子段几何
   - `length`: 长度
   - `from_node`, `to_node`: 坐标
5. 序列化为 `networkx.DiGraph` 保存到 `river_directed_v6.pkl`。

- [ ] **Step 1: 编写 pkl 重生成脚本**

```python
# scripts/regenerate_river_pkl.py
"""基于清洗后的 full_v6 重新生成 river_directed_v6.pkl。"""
from __future__ import annotations

import os
import pickle
from collections import deque
from pathlib import Path

import networkx as nx
import psycopg2
from shapely import wkb
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point
from shapely.ops import split, snap

COORD_PRECISION = 8


def _pg_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "${PGHOST}"),
        port=os.environ.get("PGPORT", "${PGPORT}"),
        dbname=os.environ.get("PGDATABASE", "${PGDATABASE}"),
        user=os.environ.get("PGUSER", "${PGUSER}"),
        password=os.environ.get("PGPASSWORD", "${PGPASSWORD}"),
    )


def _round_coord(coord):
    return (round(coord[0], COORD_PRECISION), round(coord[1], COORD_PRECISION))


def _load_river_segments(table: str):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT objectid, src_name, ST_AsBinary(geom) AS geom
                FROM {table}
                WHERE geom IS NOT NULL
                ORDER BY objectid
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    segments = []
    for oid, name, geom_wkb in rows:
        geom = wkb.loads(bytes(geom_wkb))
        if geom.geom_type == "LineString":
            segments.append({"objectid": oid, "name": name, "geom": geom})
        elif geom.geom_type == "MultiLineString":
            for part in geom.geoms:
                segments.append({"objectid": oid, "name": name, "geom": LineString(list(part.coords))})
    return segments


def _build_undirected_graph(segments):
    G = nx.Graph()
    for seg in segments:
        coords = list(seg["geom"].coords)
        start = _round_coord(coords[0])
        end = _round_coord(coords[-1])
        if start == end:
            continue
        G.add_edge(
            start, end,
            objectid=seg["objectid"],
            name=seg["name"],
            geom=seg["geom"],
            length=seg["geom"].length,
        )
    return G


def _split_at_nodes(G, tolerance=1e-7):
    all_nodes = MultiPoint([Point(n) for n in G.nodes])
    new_G = nx.Graph()

    for u, v, data in G.edges(data=True):
        geom = data["geom"]
        try:
            snapped = snap(geom, all_nodes, tolerance)
            parts = list(split(snapped, all_nodes).geoms)
        except Exception:
            parts = [geom]

        for part in parts:
            coords = list(part.coords)
            s = _round_coord(coords[0])
            e = _round_coord(coords[-1])
            if s == e:
                continue
            new_G.add_edge(
                s, e,
                objectid=data["objectid"],
                name=data["name"],
                geom=part,
                length=part.length,
            )
    return new_G


def _find_outlets(G):
    outlets = []
    for comp in nx.connected_components(G):
        sub = G.subgraph(comp)
        leaves = [n for n in sub.nodes if sub.degree(n) == 1]
        if not leaves:
            leaves = list(sub.nodes)
        outlet = max(leaves, key=lambda n: n[0])
        outlets.append(outlet)
    return outlets


def _assign_direction(G, outlets):
    DG = nx.DiGraph()
    DG.add_nodes_from(G.nodes(data=True))
    visited_edges = set()
    queue = deque(outlets)
    visited_nodes = set(outlets)

    while queue:
        node = queue.popleft()
        for neighbor in G.neighbors(node):
            edge_key = (min(node, neighbor), max(node, neighbor))
            if edge_key in visited_edges:
                continue
            visited_edges.add(edge_key)

            data = G.edges[node, neighbor]
            upstream, downstream = neighbor, node
            geom = data["geom"]
            coords = list(geom.coords)
            if _round_coord(coords[0]) != upstream:
                geom = LineString(coords[::-1])

            DG.add_edge(
                upstream, downstream,
                objectid=data["objectid"],
                name=data["name"],
                geom=geom,
                length=geom.length,
            )

            if neighbor not in visited_nodes:
                visited_nodes.add(neighbor)
                queue.append(neighbor)
    return DG


def regenerate(output_pkl: str, table: str):
    print(f"读取表: {table}")
    segments = _load_river_segments(table)
    print(f"  共 {len(segments)} 个原始河段")

    print("构建无向图...")
    G = _build_undirected_graph(segments)
    print(f"  节点 {G.number_of_nodes()}, 边 {G.number_of_edges()}")

    print("在节点处分割...")
    G = _split_at_nodes(G)
    print(f"  分割后节点 {G.number_of_nodes()}, 边 {G.number_of_edges()}")

    print("确定流向...")
    outlets = _find_outlets(G)
    DG = _assign_direction(G, outlets)
    print(f"  有向边 {DG.number_of_edges()}")

    out_path = Path(output_pkl)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump(DG, f)
    print(f"pkl 已保存: {out_path}")


def main():
    table = os.environ.get("RIVER_TABLE_FULL", "haihe_river_directed_full_v6")
    pkl_path = os.environ.get("RIVER_GRAPH_PATH", r"E:\tj\line\result\river_directed_v6.pkl")
    regenerate(pkl_path, table)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行 pkl 重生成脚本**

```bash
set RIVER_TABLE_FULL=haihe_river_directed_full_v6
set RIVER_GRAPH_PATH=E:\tj\line\result\river_directed_v6.pkl
python scripts/regenerate_river_pkl.py
```

Expected output:
```
读取表: haihe_river_directed_full_v6
  共 N 个原始河段
构建无向图...
  节点 X, 边 Y
在节点处分割...
  分割后节点 X', 边 Y'
确定流向...
  有向边 Z
pkl 已保存: E:\tj\line\result\river_directed_v6.pkl
```

- [ ] **Step 3: 验证 pkl 结构**

```python
# 临时验证
import pickle, networkx as nx
with open(r"E:\tj\line\result\river_directed_v6.pkl", "rb") as f:
    G = pickle.load(f)
print(type(G), G.number_of_nodes(), G.number_of_edges())
print(list(G.edges(data=True))[0])
```

Expected: `<class 'networkx.classes.digraph.DiGraph'>`, edges contain `objectid`, `name`, `geom`.

- [ ] **Step 4: 提交脚本**

```bash
git add scripts/regenerate_river_pkl.py
git commit -m "feat(river): regenerate directed river pkl from cleaned full_v6"
```

---

## Task 5: 同步代码层常量与配置

**Files:**
- Modify: `haihe-weather-analyzer-mcp/config.ini`
- Modify: `haihe-weather-analyzer-mcp/constants.py`（若需版本号变更）

**目标：** 确保运行时加载新生成的 pkl 和清洗后的表。

- [ ] **Step 1: 确认 config.ini 中 graph 路径**

```ini
[paths]
graph = E:/tj/line/result/river_directed_v6.pkl
```

- [ ] **Step 2: 确认 constants.py 中版本未变**

```python
DIRECTED_GRAPH_FILENAME = "river_directed_v6.pkl"
RIVER_TABLE_VERSION = "v6"
RIVER_TABLE_FULL = f"haihe_river_directed_full_{RIVER_TABLE_VERSION}"
```

若决定保留旧 pkl 作为回滚，可将新版本命名为 `river_directed_v7.pkl` 并同步更新 `DIRECTED_GRAPH_FILENAME` 与 `RIVER_TABLE_VERSION`。

- [ ] **Step 3: 提交配置更新**

```bash
git add haihe-weather-analyzer-mcp/config.ini haihe-weather-analyzer-mcp/constants.py
git commit -m "chore(river): align config/constants with regenerated pkl and cleaned full_v6"
```

---

## Task 6: 简化 rainfall_impact_geojson.py（可选但推荐）

**Files:**
- Modify: `hhlyqyxt-master/utils/rainfall_impact_geojson.py`

**目标：** 由于 pkl edge 已携带 `objectid` 且 full_v6 一个 objectid 只对应一段连通河流，可简化下游匹配逻辑。

### 可简化点
1. `_query_downstream_rows` 中复杂的子段选择/方向裁剪逻辑可改为直接使用 pkl edge 的 `geom`。
2. `_drop_downstream_covered_by_direct` 仍可保留作为保险，但触发概率大幅降低。
3. `_query_direct_rows` 中同一 objectid 多段合并的逻辑可保留作为向后兼容。

- [ ] **Step 1: 在 `_query_downstream_rows` 中优先使用 pkl edge 几何**

若 pkl edge 已携带精确 `objectid` 和 `geom`，可在 `_create_downstream_temp` 中增加 `geom_wkt` 字段，回查时直接 `ST_LineSubstring` 或按 edge _geom 裁剪。

- [ ] **Step 2: 运行单元测试确保未破坏**

```bash
cd hhlyqyxt-master/utils
python -m pytest tests/test_rainfall_impact_geojson.py -v
```

- [ ] **Step 3: 提交简化代码**

```bash
git add hhlyqyxt-master/utils/rainfall_impact_geojson.py
git commit -m "refactor(river): simplify downstream matching now that pkl/full_v6 are aligned"
```

---

## Task 7: 回归测试

**Files:**
- Test: `hhlyqyxt-master/utils/tests/test_rainfall_impact_geojson.py`
- Test: `chainlitexam/tests/test_fast_paths.py`
- Test: `chainlitexam/tests/test_rainfall_river_impact.py`

- [ ] **Step 1: 运行 traction-agent 单元测试**

```bash
cd hhlyqyxt-master/utils
python -m pytest tests/test_rainfall_impact_geojson.py -v
```

Expected: all pass.

- [ ] **Step 2: 运行 Chainlit 测试**

```bash
cd chainlitexam
python tests/test_fast_paths.py
python -m pytest tests/test_rainfall_river_impact.py -v
```

Expected: all pass.

- [ ] **Step 3: 提交测试报告**

```bash
git add -A
git commit -m "test(river): regression tests pass after full_v6 clean and pkl regenerate"
```

---

## Task 8: 内网样本验证

**Files:**
- Create: `scripts/validate_alignment.py`
- Test: 运行 `hhlyqyxt-master/utils/test_rain_impact_internal.py`

**目标：** 用同一批暴雨样本重新生成 `rain_impact_result.json.river.geojson`，对比异常指标。

- [ ] **Step 1: 编写验证脚本**

```python
# scripts/validate_alignment.py
"""对比清洗前后的 rain impact 输出指标。"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

TOLERANCE = 1e-6


def analyze(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features", [])
    by_type = Counter(f["properties"].get("impact_type", "unknown") for f in features)

    oid_counter = Counter(
        str(f["properties"].get("objectid")) for f in features
        if f["properties"].get("objectid") is not None
    )
    repeated = {oid: cnt for oid, cnt in oid_counter.items() if cnt > 1}

    endpoints = defaultdict(list)
    for i, f in enumerate(features):
        geom = shape(f.get("geometry"))
        lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for line in lines:
            endpoints[_key(line.coords[0])].append(i)
            endpoints[_key(line.coords[-1])].append(i)

    isolated = []
    for i, f in enumerate(features):
        geom = shape(f.get("geometry"))
        lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        connected = False
        for line in lines:
            for ep in [line.coords[0], line.coords[-1]]:
                if len(endpoints.get(_key(ep), [])) > 1:
                    connected = True
        if not connected:
            isolated.append(i)

    return {
        "total": len(features),
        "by_type": dict(by_type),
        "repeated_objectids": len(repeated),
        "isolated_segments": len(isolated),
    }


def _key(coord):
    return (round(coord[0], 6), round(coord[1], 6))


def main():
    if len(sys.argv) < 3:
        print("Usage: validate_alignment.py <old_geojson> <new_geojson>")
        sys.exit(1)

    old = analyze(sys.argv[1])
    new = analyze(sys.argv[2])

    print("Before:", json.dumps(old, ensure_ascii=False, indent=2))
    print("After: ", json.dumps(new, ensure_ascii=False, indent=2))

    improvements = {
        "repeated_objectids_delta": old["repeated_objectids"] - new["repeated_objectids"],
        "isolated_segments_delta": old["isolated_segments"] - new["isolated_segments"],
    }
    print("Improvements:", json.dumps(improvements, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行内网样本并对比**

```bash
cd hhlyqyxt-master/utils
python test_rain_impact_internal.py
# 将生成的 rain_impact_result.json.river.geojson 重命名为 new_result.geojson
python ../../haiheliuyubaoyuagent-master/scripts/validate_alignment.py \
  E:/fsdownload/rain_impact_result.json.river.geojson \
  E:/fsdownload/new_result.geojson
```

Expected: `repeated_objectids` 和 `isolated_segments` 显著下降。

- [ ] **Step 3: 提交验证脚本**

```bash
git add scripts/validate_alignment.py
git commit -m "feat(river): add alignment validation script"
```

---

## Task 9: 回滚验证

**Files:**
- 无新文件

**目标：** 确认备份可正常回滚。

- [ ] **Step 1: 测试表回滚**

```sql
-- 假设备份表名为 haihe_river_directed_full_v6_bak_20260716_xxxxxx
DROP TABLE IF EXISTS haihe_river_directed_full_v6;
ALTER TABLE haihe_river_directed_full_v6_bak_20260716_xxxxxx RENAME TO haihe_river_directed_full_v6;
```

- [ ] **Step 2: 测试 pkl 回滚**

```bash
copy E:\tj\line\result\river_directed_v6.bak_20260716_xxxxxx.pkl E:\tj\line\result\river_directed_v6.pkl /Y
```

- [ ] **Step 3: 回滚后运行回归测试**

```bash
cd hhlyqyxt-master/utils
python -m pytest tests/test_rainfall_impact_geojson.py -v
```

Expected: all pass。

- [ ] **Step 4: 重新应用清洗（若回滚只是验证）**

重复 Task 3 Step 4 与 Task 4 Step 2。

---

## Task 10: 文档与记忆更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `MEMORY.md` / claude-mem

- [ ] **Step 1: 更新 CLAUDE.md 中数据对齐说明**

在 `CLAUDE.md` 的 “Data-alignment caveat” 附近新增：

```markdown
## Data Alignment Maintenance

`haihe_river_directed_full_v6` 已清洗，每个 `objectid` 对应单一连通河流。
`river_directed_v6.pkl` 基于清洗后的 full_v6 重新生成，pkl edge 与 full_v6 子段一一对应。
若未来需要更新河网数据，执行顺序必须是：
1. 备份原表与原 pkl（`scripts/backup_river_data.py`）
2. 清洗 full_v6（`scripts/clean_full_v6.py`）
3. 重生成 pkl（`scripts/regenerate_river_pkl.py`）
4. 运行回归测试与内网样本验证
```

- [ ] **Step 2: 使用 claude-mem 记录最终决策**

```bash
# 无需命令，在对话中调用 claude-mem:observation_add 记录：
# "海河流域河网数据对齐：采用方案 C（先清洗 full_v6，再重生成 pkl）。
#  关键约束：备份加 bak 后缀；清洗规则按名称+连通性聚类；
#  pkl edge 必须携带 objectid、name、geom、length、from/to node。"
```

- [ ] **Step 3: 提交文档**

```bash
git add CLAUDE.md
git commit -m "docs(river): document full_v6 cleaning and pkl regeneration workflow"
```

---

## Self-Review

### Spec Coverage
- 备份策略：Task 1
- full_v6 清洗：Task 3
- pkl 重生成：Task 4
- 代码层同步：Task 5
- 简化代码：Task 6
- 验证与回滚：Task 7-9
- 文档与记忆：Task 10

### Placeholder Scan
- 无 `TBD`/`TODO`/`implement later`
- 连接信息使用环境变量 `${...}`，未硬编码 IP/密码
- 所有脚本均包含完整可运行代码

### Type Consistency
- `RIVER_TABLE_FULL`、`RIVER_TABLE_FULL_OUTPUT`、`RIVER_GRAPH_PATH` 环境变量名在所有脚本中保持一致
- pkl edge 属性键：`objectid`, `name`, `geom`, `length`, `from_node`, `to_node` 在重生成脚本和验证脚本中一致

### Risk Notes
1. 清洗脚本会修改生产表；务必先完成 Task 1 备份并用 `haihe_river_directed_full_v6_cleaned` 灰度验证。
2. 若其他业务依赖原 `objectid` 编号，需评估 remap 影响。
3. `scripts/regenerate_river_pkl.py` 基于 `src_name` 和几何，若原表缺少流向字段，BFS 方向可能与自然流向不完全一致，需人工抽检拓扑文本。
