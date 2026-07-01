from __future__ import annotations

import configparser
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

LEVEL_TO_LABEL = {"I": "一级", "II": "二级", "III": "三级", "IV": "四级"}
LEVEL_RANK = {"I": 1, "II": 2, "III": 3, "IV": 4}
ARCHIVED_STATUSES = frozenset(s.lower() for s in ("archived", "closed", "ended", "done"))

# 大屏时间轴/应急响应列表四象限（相对 now_time 计算；也可在 ext.timeline_phase 强制指定）
TIMELINE_PHASE_KEYS = frozenset({"past", "now", "ongoing", "future_hours"})
TIMELINE_PHASE_LABELS = {
    "past": "过去",
    "now": "现在",
    "ongoing": "正在发生",
    "future_hours": "几小时后",
}
DEFAULT_NOW_WINDOW_HOURS = 2.0
AUTO_ARCHIVE_GRACE_HOURS = 3.0


def _to_iso_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y-%m-%d %H",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _fmt_ts(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _status_display(status: Optional[str]) -> str:
    s = (status or "").strip().lower()
    if s in ARCHIVED_STATUSES:
        return "已归档"
    return "持续中"


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    iso = _to_iso_time(str(value))
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_ext(raw: Any) -> Dict[str, Any]:
    """JSONB 在部分环境下可能为 str，统一为 dict 供 workflow / response_ui 使用。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _merge_event_ext_for_upsert(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """
    合并事件 ext，避免 upsert 时把 workflow/publish_ack 等上下文记忆覆盖丢失。
    规则：
    - 顶层字段以 incoming 为主；
    - workflow 使用“旧 + 新”浅合并，确保 publish_ack 等历史状态可保留。
    """
    base = dict(existing or {})
    inc = dict(incoming or {})
    merged = {**base, **inc}
    old_wf = base.get("workflow")
    new_wf = inc.get("workflow")
    if isinstance(old_wf, dict) or isinstance(new_wf, dict):
        wf_old = old_wf if isinstance(old_wf, dict) else {}
        wf_new = new_wf if isinstance(new_wf, dict) else {}
        merged["workflow"] = {**wf_old, **wf_new}
    return merged


def _resolve_effective_range(event: Dict[str, Any]) -> tuple[Optional[datetime], Optional[datetime]]:
    ext = _coerce_ext(event.get("ext"))
    start_dt = _parse_iso_datetime(ext.get("effective_start_time"))
    if start_dt is None:
        delay_hours = _safe_float(ext.get("effective_delay_hours"))
        if delay_hours is not None:
            base_dt = (
                _parse_iso_datetime(ext.get("base_time"))
                or _parse_iso_datetime(ext.get("forecast_time"))
                or _parse_iso_datetime(event.get("start_time"))
            )
            if base_dt is not None:
                start_dt = base_dt + timedelta(hours=delay_hours)
    if start_dt is None:
        start_dt = _parse_iso_datetime(event.get("start_time"))

    end_dt = _parse_iso_datetime(ext.get("effective_end_time")) or _parse_iso_datetime(event.get("end_time"))
    return start_dt, end_dt


def _row_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    level = (row.get("event_level") or "").strip().upper()
    st = row.get("status") or ""
    ext = _coerce_ext(row.get("ext"))
    wf = ext.get("workflow") if isinstance(ext.get("workflow"), dict) else {}
    ack = wf.get("publish_ack") if isinstance(wf.get("publish_ack"), dict) else {}
    ack_status = str(ack.get("status") or "").strip().lower()
    answered_at = _fmt_ts(ack.get("answered_at"))
    if ack_status in ("yes", "no"):
        publish_status = ack_status
    elif answered_at:
        publish_status = "yes" if bool(ack.get("published")) else "no"
    else:
        publish_status = "pending"

    out = {
        "id": row.get("id"),
        "event_code": row.get("event_code"),
        "title": row.get("title"),
        "event_type": row.get("event_type"),
        "status": st,
        "status_display": _status_display(st),
        "start_time": _fmt_ts(row.get("start_time")),
        "end_time": _fmt_ts(row.get("end_time")),
        "event_level": level or row.get("event_level"),
        "level_display": LEVEL_TO_LABEL.get(level, row.get("event_level")),
        "latest_cycle_id": row.get("latest_cycle_id"),
        "ext": ext,
        "publish_status": publish_status,
        "publish_status_display": (
            "已发布" if publish_status == "yes" else "未发布" if publish_status == "no" else "待确认"
        ),
        "publish_ack": {
            "published": True if publish_status == "yes" else False if publish_status == "no" else None,
            "status": publish_status,
            "answered_at": answered_at,
            "note": (ack.get("note") or ""),
        },
        "created_at": _fmt_ts(row.get("created_at")),
        "updated_at": _fmt_ts(row.get("updated_at")),
    }
    eff_start, eff_end = _resolve_effective_range(out)
    out["effective_start_time"] = _fmt_ts(eff_start)
    out["effective_end_time"] = _fmt_ts(eff_end)
    return out


def _segment_key(seg: Dict[str, Any]) -> tuple:
    return (
        seg.get("status"),
        seg.get("level"),
        seg.get("event_id"),
        seg.get("event_code"),
        seg.get("event_type"),
        seg.get("title"),
    )


def classify_timeline_phase(
    event: Dict[str, Any],
    now_dt: datetime,
    *,
    now_window_hours: float = DEFAULT_NOW_WINDOW_HOURS,
) -> str:
    """
    将单条应急事件归入四象限之一，供列表与时间轴节点着色：
      - past：已归档/已结束，或有效时段已完全结束
      - future_hours：有效开始晚于当前时刻（几小时后）
      - now：已进入有效时段且处于过程起始后的短窗口内（时间轴「现在」钉附近）
      - ongoing：已进入有效时段且超出上述短窗口（正在发生）
    """
    ext = _coerce_ext(event.get("ext"))
    forced = (ext.get("timeline_phase") or "").strip()
    if forced in TIMELINE_PHASE_KEYS:
        return forced

    status = (event.get("status") or "").strip().lower()
    if status in ARCHIVED_STATUSES:
        return "past"

    eff_start, eff_end = _resolve_effective_range(event)
    if eff_start is None:
        return "ongoing"

    if eff_end is not None and eff_end <= now_dt:
        return "past"

    if eff_start > now_dt:
        return "future_hours"

    win = timedelta(hours=max(0.0, float(now_window_hours)))
    if (now_dt - eff_start) <= win:
        return "now"
    return "ongoing"


def _build_ui_dialog(event: Dict[str, Any]) -> Dict[str, Any]:
    ext = _coerce_ext(event.get("ext"))
    ui = ext.get("response_ui") if isinstance(ext.get("response_ui"), dict) else {}
    title = (ui.get("dialog_title") or event.get("title") or "").strip()
    body = (ui.get("dialog_body") or "").strip()
    if not body:
        parts = [event.get("level_display") or "", (event.get("status_display") or "").strip()]
        body = " ".join(p for p in parts if p).strip() or "（无补充说明）"
    return {
        "title": title,
        "body": body,
        "list_summary": ui.get("list_summary"),
        "hint": ui.get("hint"),
    }


def _build_timeline_axis_ticks(
    now_dt: datetime,
    history_hours: int,
    future_hours: int,
    *,
    tick_step_hours: int = 12,
) -> Dict[str, Any]:
    """
    底部时间轴刻度文案（与常见大屏一致：每 tick_step 一小时一格，「过去N小时」「现在」「未来N小时」）。
    供前端直接渲染菱形刻度下文字，无需自行拼中文。
    """
    step = max(1, min(int(tick_step_hours), 48))
    oh = min(max(0, int(history_hours)), 24 * 14)
    fh = min(max(1, int(future_hours)), 24 * 14)
    ticks: List[Dict[str, Any]] = []

    for t in range(step, oh + 1, step):
        off = -t
        at = now_dt + timedelta(hours=off)
        ticks.append(
            {
                "at_time": _fmt_ts(at),
                "offset_hours_from_now": off,
                "label": f"过去{t}小时",
                "kind": "past",
            }
        )

    ticks.append(
        {
            "at_time": _fmt_ts(now_dt),
            "offset_hours_from_now": 0,
            "label": "现在",
            "kind": "now",
        }
    )

    for t in range(step, fh + 1, step):
        at = now_dt + timedelta(hours=t)
        ticks.append(
            {
                "at_time": _fmt_ts(at),
                "offset_hours_from_now": t,
                "label": f"未来{t}小时",
                "kind": "future",
            }
        )

    return {
        "tick_step_hours": step,
        "ticks": ticks,
    }


class EmergencyManagementStore:
    """
    应急管理主表 hh_emergency_event（与 tools.upsert_emergency_event_management 一致），
    供前端列表/详情/归档使用。
    """

    def __init__(self, config_path: str):
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")
        if "postgres" not in self.config:
            raise RuntimeError(f"{config_path} 缺少 [postgres] 配置")
        pg = self.config["postgres"]
        self.schema = pg.get("schema", "public")
        self._conn_kwargs = {
            "host": pg.get("host"),
            "port": int(pg.get("port", "5432")),
            "dbname": pg.get("dbname"),
            "user": pg.get("user"),
            "password": pg.get("password"),
            "sslmode": pg.get("sslmode", "disable"),
            "connect_timeout": int(pg.get("connect_timeout", "5")),
        }
        self.ensure_tables()

    def _connect(self):
        return psycopg2.connect(**self._conn_kwargs)

    def ensure_tables(self) -> None:
        """与 tools._ensure_emergency_tables 中事件相关表保持一致（幂等）。"""
        schema = self.schema
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {schema}.hh_emergency_forecast_cycle (
                        id BIGSERIAL PRIMARY KEY,
                        cycle_key TEXT NOT NULL UNIQUE,
                        forecast_time TIMESTAMP NOT NULL,
                        source_kind TEXT NOT NULL DEFAULT 'forecast',
                        trigger_id TEXT,
                        ext JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW()
                    );
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {schema}.hh_emergency_event (
                        id BIGSERIAL PRIMARY KEY,
                        event_code TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL DEFAULT 'rainstorm',
                        event_level TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        start_time TIMESTAMP NOT NULL,
                        end_time TIMESTAMP,
                        latest_cycle_id BIGINT REFERENCES {schema}.hh_emergency_forecast_cycle(id),
                        ext JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                    );
                    """
                )
            conn.commit()

    def list_events(
        self,
        page: int = 1,
        page_size: int = 20,
        range_start: Optional[str] = None,
        range_end: Optional[str] = None,
        status: str = "",
        event_type: str = "",
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), 200)
        offset = (page - 1) * page_size

        wheres: List[str] = []
        params: Dict[str, Any] = {"limit": page_size, "offset": offset}

        if range_start:
            wheres.append("e.start_time >= %(range_start)s")
            params["range_start"] = range_start
        if range_end:
            wheres.append("e.start_time <= %(range_end)s")
            params["range_end"] = range_end
        if status:
            wheres.append("LOWER(TRIM(e.status)) = %(status)s")
            params["status"] = str(status).strip().lower()
        if event_type:
            wheres.append("LOWER(TRIM(e.event_type)) = %(event_type)s")
            params["event_type"] = str(event_type).strip().lower()

        where_sql = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        schema = self.schema

        sql_count = f"SELECT COUNT(*) AS c FROM {schema}.hh_emergency_event e {where_sql}"
        sql_list = f"""
        SELECT e.id, e.event_code, e.event_type, e.event_level, e.title, e.status,
               e.start_time, e.end_time, e.latest_cycle_id, e.ext, e.created_at, e.updated_at
        FROM {schema}.hh_emergency_event e
        {where_sql}
        ORDER BY e.start_time DESC, e.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """

        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql_count, params)
                total = int(cur.fetchone()["c"])
                cur.execute(sql_list, params)
                rows = cur.fetchall()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "list": [_row_to_api(dict(r)) for r in rows],
        }

    def get_by_id_or_code(self, id_or_code: str) -> Optional[Dict[str, Any]]:
        id_or_code = (id_or_code or "").strip()
        if not id_or_code:
            return None
        schema = self.schema
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if id_or_code.isdigit():
                    cur.execute(
                        f"""
                        SELECT id, event_code, event_type, event_level, title, status,
                               start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                        FROM {schema}.hh_emergency_event
                        WHERE id = %(id)s
                        LIMIT 1
                        """,
                        {"id": int(id_or_code)},
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT id, event_code, event_type, event_level, title, status,
                               start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                        FROM {schema}.hh_emergency_event
                        WHERE event_code = %(code)s
                        LIMIT 1
                        """,
                        {"code": id_or_code},
                    )
                row = cur.fetchone()
        return _row_to_api(dict(row)) if row else None

    def find_events_by_trace_id(self, trace_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        tid = (trace_id or "").strip()
        if not tid:
            return []
        lim = max(1, min(int(limit), 500))
        schema = self.schema
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT id, event_code, event_type, event_level, title, status,
                           start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                    FROM {schema}.hh_emergency_event
                    WHERE ext->>'trace_id' = %(trace_id)s
                    ORDER BY start_time DESC, id DESC
                    LIMIT %(limit)s
                    """,
                    {"trace_id": tid, "limit": lim},
                )
                rows = cur.fetchall() or []
        return [_row_to_api(dict(r)) for r in rows]

    def build_timeline(
        self,
        now_time: Optional[str] = None,
        history_hours: int = 24,
        future_hours: int = 48,
        event_type: str = "",
        include_archived: bool = True,
        tick_step_hours: int = 12,
        limit: int = 200,
        created_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        history = max(0, int(history_hours))
        future = max(1, int(future_hours))
        now_dt = _parse_iso_datetime(now_time) if now_time else datetime.now().replace(minute=0, second=0, microsecond=0)
        if now_dt is None:
            raise ValueError("now_time 格式错误，支持 YYYY-MM-DD HH:MM[:SS] / YYYYMMDDHH")

        display_start = now_dt - timedelta(hours=history)
        display_end = now_dt + timedelta(hours=future)
        schema = self.schema

        wheres: List[str] = [
            "e.start_time <= %(display_end)s",
            "(e.end_time IS NULL OR e.end_time >= %(display_start)s)",
        ]
        params: Dict[str, Any] = {
            "display_start": display_start.strftime("%Y-%m-%d %H:%M:%S"),
            "display_end": display_end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if event_type:
            wheres.append("LOWER(TRIM(e.event_type)) = %(event_type)s")
            params["event_type"] = str(event_type).strip().lower()
        if not include_archived:
            wheres.append("LOWER(TRIM(e.status)) NOT IN %(archived)s")
            params["archived"] = tuple(ARCHIVED_STATUSES)

        ca_iso = _to_iso_time(str(created_after).strip()) if created_after else None
        if ca_iso:
            wheres.append("e.created_at >= %(created_after)s::timestamp")
            params["created_after"] = ca_iso

        where_sql = f"WHERE {' AND '.join(wheres)}"
        # 先取最新事件（DESC + LIMIT），再在内存里翻转为时间正序，避免旧演示数据长期占满 limit。
        sql = f"""
        SELECT e.id, e.event_code, e.event_type, e.event_level, e.title, e.status,
               e.start_time, e.end_time, e.latest_cycle_id, e.ext, e.created_at, e.updated_at
        FROM {schema}.hh_emergency_event e
        {where_sql}
        ORDER BY e.start_time DESC, e.id DESC
        LIMIT %(limit)s
        """
        params["limit"] = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        rows = list(reversed(rows))

        events: List[Dict[str, Any]] = []
        active_windows: List[Dict[str, Any]] = []
        for row in rows:
            api_row = _row_to_api(dict(row))
            eff_start = _parse_iso_datetime(api_row.get("effective_start_time"))
            eff_end = _parse_iso_datetime(api_row.get("effective_end_time")) or display_end
            if eff_start is None:
                continue
            if eff_end <= display_start or eff_start >= display_end:
                continue
            clipped_start = max(eff_start, display_start)
            clipped_end = min(eff_end, display_end)
            if clipped_end <= clipped_start:
                continue
            phase = classify_timeline_phase(api_row, now_dt)
            events.append(
                {
                    **api_row,
                    "clip_start_time": _fmt_ts(clipped_start),
                    "clip_end_time": _fmt_ts(clipped_end),
                    "timeline_phase": phase,
                    "timeline_phase_display": TIMELINE_PHASE_LABELS.get(phase, phase),
                    "ui_dialog": _build_ui_dialog(api_row),
                }
            )
            active_windows.append(
                {
                    "clip_start": clipped_start,
                    "clip_end": clipped_end,
                    "event": api_row,
                }
            )

        boundaries = {display_start, display_end}
        for item in active_windows:
            boundaries.add(item["clip_start"])
            boundaries.add(item["clip_end"])
        sorted_bounds = sorted(boundaries)

        segments: List[Dict[str, Any]] = []
        for idx in range(len(sorted_bounds) - 1):
            seg_start = sorted_bounds[idx]
            seg_end = sorted_bounds[idx + 1]
            if seg_end <= seg_start:
                continue
            mid = seg_start + (seg_end - seg_start) / 2
            covers = [
                item["event"]
                for item in active_windows
                if item["clip_start"] <= mid < item["clip_end"]
            ]
            if not covers:
                seg = {
                    "start_time": _fmt_ts(seg_start),
                    "end_time": _fmt_ts(seg_end),
                    "status": "normal",
                }
            else:
                chosen = sorted(
                    covers,
                    key=lambda e: (
                        LEVEL_RANK.get(str(e.get("event_level") or "").upper(), 99),
                        _parse_iso_datetime(e.get("effective_start_time")) or display_start,
                        int(e.get("id") or 0),
                    ),
                )[0]
                seg = {
                    "start_time": _fmt_ts(seg_start),
                    "end_time": _fmt_ts(seg_end),
                    "status": "response",
                    "level": chosen.get("event_level"),
                    "event_id": chosen.get("id"),
                    "event_code": chosen.get("event_code"),
                    "event_type": chosen.get("event_type"),
                    "title": chosen.get("title"),
                    "source": (chosen.get("ext") or {}).get("source"),
                }
            if segments and _segment_key(segments[-1]) == _segment_key(seg):
                segments[-1]["end_time"] = seg["end_time"]
            else:
                segments.append(seg)

        tstep = max(1, min(int(tick_step_hours), 48))
        axis = _build_timeline_axis_ticks(now_dt, history, future, tick_step_hours=tstep)

        return {
            "now_time": _fmt_ts(now_dt),
            "display_start_time": _fmt_ts(display_start),
            "display_end_time": _fmt_ts(display_end),
            "history_hours": history,
            "future_hours": future,
            "tick_step_hours": axis["tick_step_hours"],
            "axis_ticks": axis["ticks"],
            "event_count": len(events),
            "events": events,
            "segments": segments,
        }

    def build_response_board(
        self,
        now_time: Optional[str] = None,
        history_hours: int = 36,
        future_hours: int = 72,
        event_type: str = "",
        include_archived: bool = True,
        now_window_hours: float = DEFAULT_NOW_WINDOW_HOURS,
        tick_step_hours: int = 12,
        limit: int = 200,
        created_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        聚合「四象限列表 + 时间轴 + 问答区默认文案/快捷问 + 轮询说明」，供前端 Ajax 单次拉取或定时刷新。
        """
        # 自动归档：有效结束后持续 3 小时仍未归档的 active 事件，自动转 archived。
        self.auto_archive_elapsed_events(grace_hours=AUTO_ARCHIVE_GRACE_HOURS)
        history_hours = min(max(0, int(history_hours)), 24 * 14)
        future_hours = min(max(1, int(future_hours)), 24 * 14)
        tstep = max(1, min(int(tick_step_hours), 48))
        tl = self.build_timeline(
            now_time=now_time,
            history_hours=history_hours,
            future_hours=future_hours,
            event_type=event_type,
            include_archived=include_archived,
            tick_step_hours=tstep,
            limit=limit,
            created_after=created_after,
        )
        now_dt = _parse_iso_datetime(tl.get("now_time"))
        events_out: List[Dict[str, Any]] = []
        groups: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("past", "now", "ongoing", "future_hours")}
        for ev in tl.get("events") or []:
            api_core = {k: v for k, v in ev.items() if k not in ("timeline_phase", "timeline_phase_display", "ui_dialog")}
            if now_dt is not None:
                phase = classify_timeline_phase(api_core, now_dt, now_window_hours=now_window_hours)
            else:
                phase = str(ev.get("timeline_phase") or "ongoing")
            dialog = _build_ui_dialog(api_core)
            row = {
                **ev,
                "timeline_phase": phase,
                "timeline_phase_display": TIMELINE_PHASE_LABELS.get(phase, phase),
                "ui_dialog": dialog,
            }
            events_out.append(row)
            if phase in groups:
                groups[phase].append(row)

        tl_out = {**tl, "events": events_out}
        pending_publish: List[Dict[str, Any]] = []
        pending_ids: set = set()
        # 未归档且处于「现在/进行中/将到来」的条目均可能需确认是否已正式发布（含预报启动节点）
        for phase in ("now", "ongoing", "future_hours"):
            for ev in groups.get(phase) or []:
                st = (ev.get("status") or "").strip().lower()
                if st in ARCHIVED_STATUSES:
                    continue
                ext_ev = _coerce_ext(ev.get("ext"))
                wf = ext_ev.get("workflow") if isinstance(ext_ev.get("workflow"), dict) else {}
                ack = wf.get("publish_ack") if isinstance(wf.get("publish_ack"), dict) else {}
                if ack.get("answered_at") or str(ack.get("status") or "").lower() in ("yes", "no"):
                    continue
                eid = ev.get("id")
                if eid is not None and eid in pending_ids:
                    continue
                if eid is not None:
                    pending_ids.add(eid)
                pending_publish.append(
                    {
                        "event_id": ev.get("id"),
                        "event_code": ev.get("event_code"),
                        "event_level": ev.get("event_level"),
                        "level_display": ev.get("level_display"),
                        "title": ev.get("title"),
                        "timeline_phase": phase,
                        "timeline_phase_display": ev.get("timeline_phase_display"),
                    }
                )

        workflow_publish: Dict[str, Any] = {
            "step": "confirm_publication" if pending_publish else "idle",
            "prompt": (
                "请确认：下列应急响应是否已在正式渠道完成发布？（请选择 pending_events 中的条目，"
                "调用问答提交接口回复 published=true/false；用于大屏流程留痕。）"
                if pending_publish
                else None
            ),
            "pending_events": pending_publish,
            "answer_endpoint": "/emergency/management/workflow/publish-ack",
            "answer_method": "POST",
            "answer_body": {
                "event_id": "事件数字 id 或 event_code 二选一",
                "event_code": "可选，与 event_id 二选一",
                "published": True,
                "note": "可选备注",
            },
        }

        qa: Dict[str, Any] = {
            "welcome": (
                "您好，我是海河流域应急态势助手。下方为按时间阶段划分的应急响应条目，"
                "时间轴与列表会随监测与预报更新；您可直接点选快捷问题或输入关心内容。"
            ),
            "suggestions": [
                {"id": "rain_24h", "text": "未来24小时流域面雨量概况", "query_hint": "未来24小时 海河流域 降雨"},
                {"id": "rain_48h", "text": "未来48小时累计降水预报", "query_hint": "未来48小时 累计降水"},
                {"id": "rain_72h", "text": "未来72小时降雨趋势", "query_hint": "未来72小时 降雨趋势"},
                {"id": "timeline_help", "text": "时间轴上「过去/现在/正在发生/几小时后」分别表示什么？", "query_hint": "时间轴 四阶段 含义"},
            ],
            "phase_hints": {
                "past": "已结束或已归档的过程，可用于回溯研判与复盘。",
                "now": "刚进入有效影响窗口的事件，对应大屏时间轴「现在」刻度左右。",
                "ongoing": "仍在发展、尚未结束的过程，请持续关注雨量与水情。",
                "future_hours": "根据预报或预案推算的将要发生节点，供提前部署。",
            },
            "poll_interval_ms": 30000,
            "ajax": {
                "method": "GET",
                "note": "页面轮询或可见性刷新时可请求本接口，与 timeline/events 返回同一数据源口径。",
                "endpoints": {
                    "response_board": "/emergency/management/response-board",
                    "timeline": "/emergency/management/timeline",
                    "events": "/emergency/management/events",
                },
            },
            "workflow_publish": workflow_publish,
        }
        return {
            "now_time": tl_out.get("now_time"),
            "timeline_phase_labels": dict(TIMELINE_PHASE_LABELS),
            "groups": groups,
            "timeline": tl_out,
            "qa": qa,
        }

    def auto_archive_elapsed_events(self, grace_hours: float = AUTO_ARCHIVE_GRACE_HOURS) -> int:
        """
        自动归档 active 事件：
        - effective_end_time（优先 ext.effective_end_time）或 end_time 早于当前时间 grace_hours。
        """
        gh = max(0.0, float(grace_hours))
        threshold = datetime.now() - timedelta(hours=gh)
        schema = self.schema
        updated_count = 0

        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT id, ext, end_time
                    FROM {schema}.hh_emergency_event
                    WHERE LOWER(TRIM(status)) = 'active'
                    """
                )
                rows = cur.fetchall() or []
                for row in rows:
                    ext = _coerce_ext(row.get("ext"))
                    eff_end = _parse_iso_datetime(ext.get("effective_end_time")) or _parse_iso_datetime(row.get("end_time"))
                    if eff_end is None or eff_end > threshold:
                        continue
                    wf = ext.get("workflow") if isinstance(ext.get("workflow"), dict) else {}
                    wf["auto_archive"] = {
                        "enabled": True,
                        "grace_hours": gh,
                        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "reason": "effective_end_elapsed",
                    }
                    ext["workflow"] = wf
                    cur.execute(
                        f"""
                        UPDATE {schema}.hh_emergency_event
                        SET status = 'archived',
                            updated_at = NOW(),
                            ext = %(ext)s::jsonb
                        WHERE id = %(id)s
                        """,
                        {"id": row.get("id"), "ext": json.dumps(ext, ensure_ascii=False)},
                    )
                    updated_count += 1
            conn.commit()
        return updated_count

    def terminate_event(
        self,
        id_or_code: str,
        end_time: Optional[str] = None,
        status: str = "archived",
    ) -> Optional[Dict[str, Any]]:
        """归档：写入 end_time（默认当前时间）与 status（默认 archived）。"""
        id_or_code = (id_or_code or "").strip()
        if not id_or_code:
            return None
        end_ts = _to_iso_time(end_time) if end_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st = (status or "archived").strip().lower() or "archived"
        schema = self.schema

        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if id_or_code.isdigit():
                    cur.execute(
                        f"""
                        UPDATE {schema}.hh_emergency_event
                        SET end_time = %(end_ts)s::timestamp,
                            status = %(status)s,
                            updated_at = NOW()
                        WHERE id = %(id)s
                        RETURNING id, event_code, event_type, event_level, title, status,
                                  start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                        """,
                        {"end_ts": end_ts, "status": st, "id": int(id_or_code)},
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE {schema}.hh_emergency_event
                        SET end_time = %(end_ts)s::timestamp,
                            status = %(status)s,
                            updated_at = NOW()
                        WHERE event_code = %(code)s
                        RETURNING id, event_code, event_type, event_level, title, status,
                                  start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                        """,
                        {"end_ts": end_ts, "status": st, "code": id_or_code},
                    )
                row = cur.fetchone()
            conn.commit()

        return _row_to_api(dict(row)) if row else None

    def set_workflow_publish_ack(
        self,
        id_or_code: str,
        *,
        published: bool,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        状态机：记录用户对应急响应「是否已正式发布」的确认结果，写入 ext.workflow.publish_ack。
        """
        id_or_code = (id_or_code or "").strip()
        if not id_or_code:
            return None
        answered_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note_txt = (note or "").strip()
        schema = self.schema

        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if id_or_code.isdigit():
                    cur.execute(
                        f"""
                        SELECT id, event_code, event_type, event_level, title, status,
                               start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                        FROM {schema}.hh_emergency_event
                        WHERE id = %(id)s
                        LIMIT 1
                        """,
                        {"id": int(id_or_code)},
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT id, event_code, event_type, event_level, title, status,
                               start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                        FROM {schema}.hh_emergency_event
                        WHERE event_code = %(code)s
                        LIMIT 1
                        """,
                        {"code": id_or_code},
                    )
                row = cur.fetchone()
                if not row:
                    return None
                base = dict(row)
                ext = _coerce_ext(base.get("ext"))
                wf = ext.get("workflow") if isinstance(ext.get("workflow"), dict) else {}
                wf["publish_ack"] = {
                    "published": bool(published),
                    "status": "yes" if published else "no",
                    "answered_at": answered_at,
                    "note": note_txt,
                }
                wf["step"] = "publish_acknowledged"
                ext["workflow"] = wf

                eid = base.get("id")
                cur.execute(
                    f"""
                    UPDATE {schema}.hh_emergency_event
                    SET ext = %(ext)s::jsonb,
                        updated_at = NOW()
                    WHERE id = %(id)s
                    RETURNING id, event_code, event_type, event_level, title, status,
                              start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                    """,
                    {"ext": json.dumps(ext, ensure_ascii=False), "id": eid},
                )
                updated = cur.fetchone()
            conn.commit()

        return _row_to_api(dict(updated)) if updated else None

    def upsert_event(
        self,
        *,
        event_code: str,
        event_type: str,
        event_level: str,
        title: str,
        status: str,
        start_time: str,
        end_time: Optional[str] = None,
        latest_cycle_id: Optional[int] = None,
        ext: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        按 event_code 幂等写入/更新应急主表，供时间轴与 response-board 实时读取。
        """
        code = (event_code or "").strip()
        if not code:
            return None
        e_type = (event_type or "forecast").strip().lower() or "forecast"
        e_level = (event_level or "").strip().upper() or "IV"
        e_title = (title or "").strip() or f"{e_type} 应急事件"
        e_status = (status or "active").strip().lower() or "active"
        start_iso = _to_iso_time(start_time)
        end_iso = _to_iso_time(end_time) if end_time else None
        if not start_iso:
            return None
        payload_ext_incoming = ext if isinstance(ext, dict) else {}
        schema = self.schema

        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT ext
                    FROM {schema}.hh_emergency_event
                    WHERE event_code = %(event_code)s
                    LIMIT 1
                    """,
                    {"event_code": code},
                )
                old_row = cur.fetchone()
                payload_ext_existing = _coerce_ext(old_row.get("ext")) if old_row else {}
                payload_ext = _merge_event_ext_for_upsert(payload_ext_existing, payload_ext_incoming)
                cur.execute(
                    f"""
                    INSERT INTO {schema}.hh_emergency_event
                    (event_code, event_type, event_level, title, status, start_time, end_time, latest_cycle_id, ext)
                    VALUES (%(event_code)s, %(event_type)s, %(event_level)s, %(title)s, %(status)s,
                            %(start_time)s::timestamp, %(end_time)s::timestamp, %(latest_cycle_id)s, %(ext)s::jsonb)
                    ON CONFLICT (event_code) DO UPDATE
                    SET event_type = EXCLUDED.event_type,
                        event_level = EXCLUDED.event_level,
                        title = EXCLUDED.title,
                        status = EXCLUDED.status,
                        start_time = EXCLUDED.start_time,
                        end_time = EXCLUDED.end_time,
                        latest_cycle_id = EXCLUDED.latest_cycle_id,
                        ext = EXCLUDED.ext,
                        updated_at = NOW()
                    RETURNING id, event_code, event_type, event_level, title, status,
                              start_time, end_time, latest_cycle_id, ext, created_at, updated_at
                    """,
                    {
                        "event_code": code,
                        "event_type": e_type,
                        "event_level": e_level,
                        "title": e_title,
                        "status": e_status,
                        "start_time": start_iso,
                        "end_time": end_iso,
                        "latest_cycle_id": latest_cycle_id,
                        "ext": json.dumps(payload_ext, ensure_ascii=False),
                    },
                )
                row = cur.fetchone()
            conn.commit()
        return _row_to_api(dict(row)) if row else None
