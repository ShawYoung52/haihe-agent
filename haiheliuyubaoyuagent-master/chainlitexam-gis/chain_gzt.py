import io
import json
import re
import asyncio
import os
import time
import configparser
from datetime import datetime, timedelta
from uuid import uuid4
from urllib.parse import quote_plus
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import chainlit as cl
import matplotlib.pyplot as plt
import chainlit.data as cl_data
from chainlit.data import get_data_layer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.types import ThreadDict
from chainlit.user import User
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient

from prompts import WEATHER_ASSISTANT_PROMPT
from message_orchestrator import process_message

# ===============================
# 修复 Matplotlib 中文显示问题
# ===============================
plt.rcParams['font.sans-serif'] = ['SimHei']  # 用黑体显示中文
plt.rcParams['axes.unicode_minus'] = False    # 正常显示负号（因为经纬度可能有负数）

_CHAINLIT_DB_READY = False

# 启用 password_auth_callback 时，Chainlit 需要 JWT 密钥。
# 本地开发默认给一个兜底值；生产环境请改为安全随机值并走环境变量注入。
os.environ.setdefault("CHAINLIT_AUTH_SECRET", "chainlit-local-dev-secret-change-me")


def _patch_chainlit_socket_connect_auth() -> None:
    """
    兼容部分 socket.io 客户端未传 auth 的情况。
    某些版本组合下会触发：
    - TypeError: connect() missing 1 required positional argument: 'auth'
    - AttributeError: 'NoneType' object has no attribute 'get'
    """
    try:
        import chainlit.socket as chainlit_socket
    except Exception as e:
        print(f"[Chainlit] socket 兼容补丁跳过（导入失败）：{e}")
        return

    original_connect = getattr(chainlit_socket, "connect", None)
    if not callable(original_connect):
        print("[Chainlit] socket 兼容补丁跳过（未找到 connect 处理器）。")
        return

    # 防止重复打补丁
    if getattr(original_connect, "__name__", "") == "_connect_with_optional_auth":
        return

    async def _connect_with_optional_auth(sid, environ, auth=None):
        safe_auth = auth if isinstance(auth, dict) else {}
        return await original_connect(sid, environ, safe_auth)

    chainlit_socket.connect = _connect_with_optional_auth
    chainlit_socket.sio.on("connect", _connect_with_optional_auth)
    print("[Chainlit] 已应用 socket auth 兼容补丁。")


_patch_chainlit_socket_connect_auth()


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    """
    最简账号密码登录（用于开启会话历史区）。
    默认账号密码可通过环境变量覆盖：
    - CHAINLIT_AUTH_USER（默认 admin）
    - CHAINLIT_AUTH_PASSWORD（默认 admin123）
    """
    expected_user = os.getenv("CHAINLIT_AUTH_USER", "admin").strip()
    expected_password = os.getenv("CHAINLIT_AUTH_PASSWORD", "admin123")

    if username == expected_user and password == expected_password:
        return User(
            identifier=username,
            display_name="管理员",
            metadata={"role": "admin"},
        )
    return None


def _build_chainlit_postgres_conninfo() -> tuple[str, bool]:
    """
    组装 Chainlit SQLAlchemyDataLayer 连接串。
    优先使用 CHAINLIT_DB_CONNINFO，其次使用分项环境变量。
    """
    conninfo = os.getenv("CHAINLIT_DB_CONNINFO", "").strip()
    if conninfo:
        sslmode = os.getenv("CHAINLIT_DB_SSLMODE", "disable").strip().lower()
        ssl_require = sslmode in {"require", "verify-ca", "verify-full"}
        return conninfo, ssl_require

    host = os.getenv("CHAINLIT_DB_HOST", "211.157.132.19").strip()
    port = os.getenv("CHAINLIT_DB_PORT", "48091").strip()
    dbname = os.getenv("CHAINLIT_DB_NAME", "tjznt").strip()
    user = os.getenv("CHAINLIT_DB_USER", "postgres").strip()
    password = os.getenv("CHAINLIT_DB_PASSWORD", "postgres")
    sslmode = os.getenv("CHAINLIT_DB_SSLMODE", "disable").strip().lower()

    encoded_user = quote_plus(user)
    encoded_password = quote_plus(password)
    # SQLAlchemy 异步驱动，建议使用 asyncpg
    conninfo = f"postgresql+asyncpg://{encoded_user}:{encoded_password}@{host}:{port}/{dbname}"
    ssl_require = sslmode in {"require", "verify-ca", "verify-full"}
    return conninfo, ssl_require


def _init_chainlit_data_layer() -> None:
    """初始化 Chainlit 数据层（用于历史会话持久化）。"""
    if get_data_layer():
        return

    conninfo, ssl_require = _build_chainlit_postgres_conninfo()
    data_layer = SQLAlchemyDataLayer(
        conninfo=conninfo,
        ssl_require=ssl_require,
        user_thread_limit=1000,
        show_logger=True,
    )
    cl_data._data_layer = data_layer

    # 兼容历史库中 metadata 为 TEXT 的场景：
    # Chainlit 恢复线程时要求 thread.metadata 为 dict，否则会在 resume 阶段报
    # "'str' object has no attribute 'copy'"。
    original_get_all_user_threads = data_layer.get_all_user_threads

    async def _patched_get_all_user_threads(user_id: str | None = None, thread_id: str | None = None):
        rows = await original_get_all_user_threads(user_id=user_id, thread_id=thread_id)
        if not isinstance(rows, list):
            return rows

        for thread in rows:
            if not isinstance(thread, dict):
                continue

            metadata = thread.get("metadata")
            if isinstance(metadata, str):
                m = metadata.strip()
                if m:
                    try:
                        thread["metadata"] = json.loads(m)
                    except Exception:
                        thread["metadata"] = {"raw": m}
                else:
                    thread["metadata"] = {}
            elif metadata is None:
                thread["metadata"] = {}

            steps = thread.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                for key in ["metadata", "generation"]:
                    val = step.get(key)
                    if isinstance(val, str):
                        txt = val.strip()
                        if txt:
                            try:
                                step[key] = json.loads(txt)
                            except Exception:
                                step[key] = {"raw": txt}
                        else:
                            step[key] = {}
                    elif val is None:
                        step[key] = {}

                # 兼容历史/脏数据：Chainlit 在前端恢复消息时会直接读取 step["output"]。
                # 若缺失该字段会触发 KeyError: 'output'，这里统一做兜底。
                if "output" not in step or step.get("output") is None:
                    fallback_output = step.get("input")
                    step["output"] = "" if fallback_output is None else str(fallback_output)

        return rows

    data_layer.get_all_user_threads = _patched_get_all_user_threads


async def _ensure_chainlit_tables() -> None:
    """
    在 PostgreSQL 中初始化 Chainlit 历史会话基础表。
    仅首次执行。
    """
    global _CHAINLIT_DB_READY
    if _CHAINLIT_DB_READY:
        return

    data_layer = get_data_layer()
    if not isinstance(data_layer, SQLAlchemyDataLayer):
        return

    ddl_statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            "id" TEXT PRIMARY KEY,
            "identifier" TEXT UNIQUE NOT NULL,
            "createdAt" TEXT NOT NULL,
            "metadata" JSONB
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS threads (
            "id" TEXT PRIMARY KEY,
            "createdAt" TEXT,
            "name" TEXT,
            "userId" TEXT,
            "userIdentifier" TEXT,
            "tags" TEXT[],
            "metadata" JSONB
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS steps (
            "id" TEXT PRIMARY KEY,
            "name" TEXT,
            "type" TEXT,
            "threadId" TEXT,
            "parentId" TEXT,
            "streaming" BOOLEAN,
            "waitForAnswer" BOOLEAN,
            "isError" BOOLEAN,
            "metadata" JSONB,
            "tags" TEXT[],
            "input" TEXT,
            "output" TEXT,
            "createdAt" TEXT,
            "start" TEXT,
            "end" TEXT,
            "generation" JSONB,
            "showInput" TEXT,
            "language" TEXT,
            "indent" INTEGER
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS feedbacks (
            "id" TEXT PRIMARY KEY,
            "forId" TEXT,
            "threadId" TEXT,
            "value" INTEGER,
            "comment" TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS elements (
            "id" TEXT PRIMARY KEY,
            "threadId" TEXT,
            "type" TEXT NOT NULL,
            "chainlitKey" TEXT,
            "url" TEXT,
            "objectKey" TEXT,
            "name" TEXT NOT NULL,
            "display" TEXT NOT NULL,
            "size" TEXT,
            "language" TEXT,
            "page" INTEGER,
            "forId" TEXT,
            "mime" TEXT,
            "autoPlay" BOOLEAN,
            "playerConfig" TEXT
        );
        """,
        'CREATE INDEX IF NOT EXISTS idx_steps_thread_id ON steps ("threadId");',
        'CREATE INDEX IF NOT EXISTS idx_elements_thread_id ON elements ("threadId");',
        'CREATE INDEX IF NOT EXISTS idx_feedbacks_for_id ON feedbacks ("forId");',
        'CREATE INDEX IF NOT EXISTS idx_threads_user_id ON threads ("userId");',
        """
        CREATE TABLE IF NOT EXISTS gis_vector_sql_registry (
            id BIGSERIAL PRIMARY KEY,
            sql_text TEXT NOT NULL,
            scene TEXT,
            linkage_id TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        'CREATE INDEX IF NOT EXISTS idx_gis_vector_sql_registry_linkage ON gis_vector_sql_registry (linkage_id);',
        """
        ALTER TABLE users
        ALTER COLUMN "metadata" TYPE JSONB
        USING (
            CASE
                WHEN "metadata" IS NULL OR btrim("metadata"::text) = '' THEN '{}'::jsonb
                WHEN "metadata"::text LIKE '{%' OR "metadata"::text LIKE '[%' THEN "metadata"::jsonb
                ELSE jsonb_build_object('raw', "metadata"::text)
            END
        );
        """,
        """
        ALTER TABLE threads
        ALTER COLUMN "metadata" TYPE JSONB
        USING (
            CASE
                WHEN "metadata" IS NULL OR btrim("metadata"::text) = '' THEN '{}'::jsonb
                WHEN "metadata"::text LIKE '{%' OR "metadata"::text LIKE '[%' THEN "metadata"::jsonb
                ELSE jsonb_build_object('raw', "metadata"::text)
            END
        );
        """,
        """
        ALTER TABLE steps
        ALTER COLUMN "metadata" TYPE JSONB
        USING (
            CASE
                WHEN "metadata" IS NULL OR btrim("metadata"::text) = '' THEN '{}'::jsonb
                WHEN "metadata"::text LIKE '{%' OR "metadata"::text LIKE '[%' THEN "metadata"::jsonb
                ELSE jsonb_build_object('raw', "metadata"::text)
            END
        );
        """,
        """
        ALTER TABLE steps
        ALTER COLUMN "generation" TYPE JSONB
        USING (
            CASE
                WHEN "generation" IS NULL OR btrim("generation"::text) = '' THEN '{}'::jsonb
                WHEN "generation"::text LIKE '{%' OR "generation"::text LIKE '[%' THEN "generation"::jsonb
                ELSE jsonb_build_object('raw', "generation"::text)
            END
        );
        """,
    ]

    for ddl in ddl_statements:
        await data_layer.execute_sql(ddl, {})

    _CHAINLIT_DB_READY = True


if os.getenv("CHAINLIT_ENABLE_DB", "1").strip() in {"1", "true", "True"}:
    _init_chainlit_data_layer()
else:
    print("[Chainlit] CHAINLIT_ENABLE_DB 未开启，已跳过 SQLAlchemyDataLayer 初始化。")


async def astream_chain_to_message(chain, input_dict, stream_msg: cl.Message, config: RunnableConfig | None = None):
    """
    稳定优先：模型侧禁用流式（规避 Tongyi tool_calls 异常），
    前端以小块刷新实现“流式观感”。
    """
    result = await chain.ainvoke(input_dict, config=config)
    text = getattr(result, "content", None) or ""
    if text:
        chunk_size = 16
        for i in range(0, len(text), chunk_size):
            stream_msg.content += text[i:i + chunk_size]
            await stream_msg.update()
            await asyncio.sleep(0.01)
    return result


async def ainvoke_chain(chain, input_dict, config: RunnableConfig | None = None):
    """静默调用模型，用于工具决策阶段（不向前端输出中间指令）。"""
    return await chain.ainvoke(input_dict, config=config)


async def stream_text_to_message(text: str, stream_msg: cl.Message | None = None, chunk_size: int = 8, delay: float = 0.02):
    """
    统一的前端流式输出：
    - 传入现有 stream_msg：在同一条消息上持续刷新
    - 不传 stream_msg：新建一条消息并流式刷新
    """
    if stream_msg is None:
        stream_msg = cl.Message(content="")
        await stream_msg.send()

    if not text:
        return stream_msg

    for i in range(0, len(text), chunk_size):
        stream_msg.content += text[i:i + chunk_size]
        await stream_msg.update()
        await asyncio.sleep(delay)
    return stream_msg

def _user_forbids_followup(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    forbids = [
        "不要扩展", "不要追问", "别追问", "别问我", "不要问我",
        "只要结论", "只要答案", "直接给答案", "不要建议", "不需要建议",
        "不用扩展", "不用追问", "不需要追问", "别给扩展",
        "只专注这个问题", "只回答这个问题", "只说这个问题", "只围绕这个问题",
        "不要发散", "别发散", "不要展开", "别展开", "只聚焦这个问题",
    ]
    return any(k in t for k in forbids)


def _has_followup_line(text: str) -> bool:
    if not text:
        return False
    return "可继续追问：" in text


def _make_followup_question(user_text: str) -> str:
    """
    只生成 1 个强相关追问，避免泛化；不依赖二次 LLM，保证稳定。
    """
    t = (user_text or "").strip()
    if not t:
        return "可继续追问：你希望我按哪个子流域/行政区划来展开分析？"

    # 河网/水系/上下游
    if any(k in t for k in ["河网", "河道", "水系", "拓扑", "流域图", "示意图", "路径", "上下游", "汇入", "干流"]):
        return "可继续追问：你要我聚焦哪条河（或哪个子流域），以及是否需要把下游连锁影响也一起纳入？"

    # 暴雨影响/分区
    if any(k in t for k in ["暴雨", "影响范围", "分区", "区划", "246分区", "11分区", "77分区", "行政区划"]):
        return "可继续追问：你希望我按“行政区划”还是按“分区图层（如246/11/77）”输出完整清单？"

    # 降雨/雨量/预报
    if any(k in t for k in ["降雨", "雨量", "下雨", "降水", "毫米", "面雨量", "累计"]):
        return "可继续追问：你要查询的具体地点（市/县/站点）和时间范围是“今天白天/今夜/未来24小时/未来3天”中的哪一个？"

    # 时间类
    if any(k in t for k in ["今天", "明天", "后天", "周末", "本周", "下周", "现在", "当前", "几点", "日期", "时间"]):
        return "可继续追问：你要我以哪个时间段为准（例如今天白天/今夜/未来24小时），并聚焦哪个区域？"

    return "可继续追问：你更关心哪一项——降雨量大小、影响范围（区划/分区）、还是上下游传导路径？"


def _append_followup_if_needed(answer_text: str, user_text: str) -> str:
    if not answer_text:
        return answer_text
    if _user_forbids_followup(user_text):
        return answer_text
    if _has_followup_line(answer_text):
        return answer_text
    return f"{answer_text.rstrip()}\n\n可继续追问：{_make_followup_question(user_text).replace('可继续追问：','').strip()}"


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def _user_requests_strict_focus(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    focus_keywords = [
        "只专注这个问题", "只回答这个问题", "只说这个问题", "只围绕这个问题",
        "不要发散", "别发散", "不要展开", "别展开", "只聚焦这个", "只聚焦这个问题",
    ]
    return _contains_any(t, focus_keywords)


def _should_force_admin_units_reply(user_text: str) -> bool:
    """
    只在用户明确问“受影响行政区划/区县”且没有同时追问其它维度时，
    走确定性的工具结果直出，避免模型自行扩展到分区、下游、建议等内容。
    """
    if not user_text:
        return False

    t = user_text.strip()
    admin_keywords = ["行政区划", "行政区", "区县", "县区"]
    focused_admin_phrases = [
        "哪些行政区划", "哪些行政区", "涉及哪些行政区划", "涉及哪些行政区",
        "会影响哪些行政区划", "会影响哪些行政区", "影响哪些行政区划", "影响哪些行政区",
        "哪些区县", "涉及哪些区县", "会影响哪些区县", "影响哪些区县",
        "哪些县区", "涉及哪些县区", "会影响哪些县区", "影响哪些县区",
        "影响的行政区划", "暴雨影响的行政区划", "发生暴雨影响的行政区划",
        "行政区划有哪些", "行政区有哪些", "区县有哪些", "县区有哪些",
    ]
    mixed_scope_keywords = [
        "分区", "246", "11分区", "77分区", "32分区", "9分区",
        "下游", "汇入", "连锁", "传导", "哪些河", "河网", "路径", "示意图", "画图",
        "风险", "风险等级", "建议", "行动", "防御", "应对", "预案",
        "为什么", "原因", "机理", "过程",
    ]

    if not _contains_any(t, admin_keywords):
        return False
    if _contains_any(t, mixed_scope_keywords):
        return False
    if _contains_any(t, focused_admin_phrases) or _user_requests_strict_focus(t):
        return True

    # 兜底：只要明确提到“行政区划/区县”，且未混入分区/下游/建议等其它主题，就走行政区划专用收口
    broad_admin_patterns = [
        "行政区划", "行政区", "区县", "县区",
    ]
    return _contains_any(t, broad_admin_patterns)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _pick_first_text(data: dict, keys: list[str]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _dedupe_joined_parts(parts: list[str]) -> str:
    deduped = []
    for part in parts:
        if part and (not deduped or deduped[-1] != part):
            deduped.append(part)
    return "".join(deduped)


def _match_first_suffix_token(text: str, suffixes: list[str]) -> tuple[str, str]:
    """
    在 text 中匹配“最先出现”的行政后缀。
    若同一位置有多个后缀可匹配，优先更长后缀，避免把“自治州”截成“州”。
    """
    best_idx = None
    best_suffix = ""
    for s in suffixes:
        idx = text.find(s)
        if idx <= 0:
            continue
        if (
            best_idx is None
            or idx < best_idx
            or (idx == best_idx and len(s) > len(best_suffix))
        ):
            best_idx = idx
            best_suffix = s

    if best_idx is not None:
        end = best_idx + len(best_suffix)
        return text[:end], text[end:]
    return "", text


def _is_suspicious_admin_field(value: str, level: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False

    if level == "province":
        return any(k in v for k in ["区", "县", "乡", "镇", "街道"])
    if level == "city":
        return ("市" in v and v.count("市") > 1) or any(k in v for k in ["区", "县", "乡", "镇", "街道"])
    if level == "district":
        return any(k in v for k in ["省", "乡", "镇", "街道"])
    if level == "town":
        return len(v) <= 1
    return False


def _should_replace_with_parsed(existing: str, parsed: str, level: str) -> bool:
    e = (existing or "").strip()
    p = (parsed or "").strip()
    if not p:
        return False
    if not e:
        return True
    if _is_suspicious_admin_field(e, level):
        return True
    if e != p and e.startswith(p):
        # 例如“保定市涿州”应纠正为“保定市”
        return True
    return False


def _split_cn_admin_name(name: str) -> tuple[str, str, str, str]:
    """
    把类似“北京市北京市丰台区卢沟桥镇”拆成省市区乡四级。
    拆分失败的层级返回空串。
    """
    text = (name or "").strip().replace(" ", "")
    if not text:
        return "", "", "", ""

    province_suffixes = ["特别行政区", "自治区", "省", "市"]
    city_suffixes = ["自治州", "地区", "盟", "州", "市"]
    district_suffixes = ["自治县", "旗", "新区", "开发区", "管理区", "区", "县", "市"]
    town_suffixes = ["街道", "镇", "乡", "苏木"]

    province, rest = _match_first_suffix_token(text, province_suffixes)
    city, rest = _match_first_suffix_token(rest, city_suffixes)
    district, rest = _match_first_suffix_token(rest, district_suffixes)
    town, _ = _match_first_suffix_token(rest, town_suffixes)

    # 若无 town 后缀但还有残余，仍作为最末级名称保留
    if not town and rest:
        town = rest

    return province, city, district, town


def _cleanup_admin_parts(province: str, city: str, district: str, town: str) -> tuple[str, str, str, str]:
    province = (province or "").strip()
    city = (city or "").strip()
    district = (district or "").strip()
    town = (town or "").strip()

    # 城市字段里误带区县时，裁到“市/地区/盟/州”结束
    for token in ["自治州", "地区", "盟", "州", "市"]:
        idx = city.find(token)
        if idx > 0:
            city = city[: idx + len(token)]
            break

    # 区县字段里误带完整前缀时，优先取真正区县尾段
    if district and any(k in district for k in ["省", "市", "乡", "镇", "街道"]):
        _, _, parsed_district, parsed_town = _split_cn_admin_name(district)
        if parsed_district:
            district = parsed_district
        if (not town) and parsed_town:
            town = parsed_town

    # 乡镇字段若误带前缀（如“市义和庄乡”），截取最后一个乡镇后缀对应片段
    for suffix in ["街道", "镇", "乡", "苏木"]:
        idx = town.rfind(suffix)
        if idx >= 0:
            start = town[:idx].rfind("市")
            if start >= 0 and (idx - start) <= 5:
                town = town[start + 1 : idx + len(suffix)]
            break

    # 乡镇只剩“镇/乡”等单字时，视为无效
    if town in {"镇", "乡", "街道", "苏木"}:
        town = ""

    return province, city, district, town


def _normalize_admin_unit_item(item) -> dict[str, str] | None:
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        province, city, district, town = _split_cn_admin_name(name)
        return {
            "name": name,
            "code": "",
            "province": province,
            "city": city,
            "district": district,
            "town": town,
        }

    if not isinstance(item, dict):
        return None

    province = _pick_first_text(item, ["province_name", "province"])
    city = _pick_first_text(item, ["city_name", "city"])
    district = _pick_first_text(
        item,
        ["district_name", "district", "county_name", "county", "area_name"],
    )
    town = _pick_first_text(
        item,
        ["town_name", "town", "street_name", "street", "township_name", "township"],
    )

    name = _pick_first_text(
        item,
        [
            "name", "admin_name", "unit_name", "region_name", "label",
            "district_name", "county_name", "area_name",
        ],
    )
    if not name:
        name = _dedupe_joined_parts(
            [
                province,
                city,
                district,
                town,
            ]
        )
    if not name:
        for key, value in item.items():
            if key in {"code", "adcode", "admin_code", "district_code", "county_code"}:
                continue
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
    if not name:
        return None

    # 尝试从拼接名称拆层：用于补全缺失字段，也用于修复明显异常字段
    s_province, s_city, s_district, s_town = _split_cn_admin_name(name)
    if _should_replace_with_parsed(province, s_province, "province"):
        province = s_province
    if _should_replace_with_parsed(city, s_city, "city"):
        city = s_city
    if _should_replace_with_parsed(district, s_district, "district"):
        district = s_district
    if _should_replace_with_parsed(town, s_town, "town"):
        town = s_town

    province, city, district, town = _cleanup_admin_parts(province, city, district, town)

    code = _pick_first_text(
        item,
        ["code", "adcode", "admin_code", "district_code", "county_code", "region_code"],
    )
    return {
        "name": name,
        "code": code,
        "province": province,
        "city": city,
        "district": district,
        "town": town,
    }


def _extract_admin_unit_rows(raw_result) -> tuple[str, list[dict[str, str]]]:
    data = _unwrap_tool_result(raw_result)
    if not isinstance(data, dict):
        return "该河流", []

    river_name = str(data.get("river_name") or data.get("name") or "该河流").strip()
    self_report = data.get("self_report")
    report = self_report if isinstance(self_report, dict) else data

    candidates = []
    for key in ["admin_units", "admin_regions", "administrative_units", "affected_admin_units"]:
        if isinstance(report, dict) and report.get(key):
            candidates = _as_list(report.get(key))
            break
    if not candidates and isinstance(data, dict):
        for key in ["admin_units", "admin_regions", "administrative_units", "affected_admin_units"]:
            if data.get(key):
                candidates = _as_list(data.get(key))
                break

    rows = []
    seen = set()
    for item in candidates:
        row = _normalize_admin_unit_item(item)
        if not row:
            continue
        dedupe_key = (row["name"], row["code"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(row)

    rows.sort(
        key=lambda x: (
            x.get("province", ""),
            x.get("city", ""),
            x.get("district", ""),
            x.get("town", ""),
            x.get("name", ""),
            x.get("code", ""),
        )
    )
    return river_name or "该河流", rows


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _build_admin_units_only_reply(raw_result) -> str | None:
    river_name, rows = _extract_admin_unit_rows(raw_result)
    if not rows:
        return (
            f"当前工具结果中未返回 `{river_name}` 暴雨影响的行政区划清单，"
            "因此暂时无法只按“行政区划”给出确定列表。"
        )

    has_hierarchy = any(
        row.get("province") or row.get("city") or row.get("district") or row.get("town")
        for row in rows
    )

    # 层级字段存在时，按“省-市-区县”合并展示，乡镇/街道聚合到同一格
    if has_hierarchy:
        grouped = {}
        for row in rows:
            province = (row.get("province") or "—").strip() or "—"
            city = (row.get("city") or "—").strip() or "—"
            district = (row.get("district") or row.get("name") or "—").strip() or "—"
            town = (row.get("town") or "").strip()
            if not town and row.get("name") and row.get("name") not in {district, city, province}:
                town = row["name"].strip()

            key = (province, city, district)
            if key not in grouped:
                grouped[key] = {"towns": set()}
            if town:
                grouped[key]["towns"].add(town)

        total_towns = sum(len(v["towns"]) for v in grouped.values())
        lines = [
            f"按当前工具结果，`{river_name}` 暴雨影响涉及 {len(grouped)} 个区县。以下仅列行政区划，不展开分区、下游和防御建议。",
            "",
            f"| 省份 | 地市 | 区县 | 乡镇/街道（共{total_towns}个） |",
            "| :--- | :--- | :--- | :--- |",
        ]

        prev_province = None
        prev_city = None
        for province, city, district in sorted(grouped.keys()):
            towns = sorted(grouped[(province, city, district)]["towns"])
            town_text = "、".join(_escape_md_cell(t) for t in towns) if towns else "—"
            show_province = province if province != prev_province else ""
            show_city = city if (province != prev_province or city != prev_city) else ""
            lines.append(
                f"| {_escape_md_cell(show_province)} | {_escape_md_cell(show_city)} | {_escape_md_cell(district)} | {town_text} |"
            )
            prev_province = province
            prev_city = city
    else:
        include_code = any(row["code"] for row in rows)
        lines = [
            f"按当前工具结果，`{river_name}` 暴雨影响的行政区划共 {len(rows)} 个。以下仅列行政区划，不展开分区、下游和防御建议。",
            "",
        ]

        if include_code:
            lines.extend(
                [
                    "| 序号 | 行政区划 | 代码 |",
                    "| :--- | :--- | :--- |",
                ]
            )
            for idx, row in enumerate(rows, 1):
                lines.append(
                    f"| {idx} | {_escape_md_cell(row['name'])} | {_escape_md_cell(row['code'] or '-')} |"
                )
        else:
            lines.extend(
                [
                    "| 序号 | 行政区划 |",
                    "| :--- | :--- |",
                ]
            )
            for idx, row in enumerate(rows, 1):
                lines.append(f"| {idx} | {_escape_md_cell(row['name'])} |")

    return "\n".join(lines)


def _should_force_structured_impact_reply(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    if _should_force_partition_table_reply(t):
        return False
    has_rainstorm = _contains_any(t, ["暴雨", "强降雨", "降雨"])
    has_impact = _contains_any(t, ["影响", "影响范围", "会影响", "影响分析"])
    if not (has_rainstorm and has_impact):
        return False
    # 专门问行政区划时走“行政区划专用收口”
    if _should_force_admin_units_reply(t):
        return False
    return True


def _should_force_partition_table_reply(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    partition_keywords = ["分区表", "影响分区", "分区明细", "分区清单", "分区情况", "分区"]
    admin_keywords = ["行政区划", "行政区", "区县", "县区", "乡镇", "街道"]
    if not _contains_any(t, partition_keywords):
        return False
    if _contains_any(t, admin_keywords):
        return False
    return True


def _extract_partition_groups(raw_result) -> dict[str, list[dict[str, str]]]:
    data = _unwrap_tool_result(raw_result)
    report = data.get("self_report") if isinstance(data, dict) and isinstance(data.get("self_report"), dict) else data
    if not isinstance(report, dict):
        return {}

    groups: dict[str, list[dict[str, str]]] = {
        "海河246分区": [],
        "海河11分区": [],
        "海河32分区": [],
        "海河77分区": [],
        "海河9分区": [],
    }

    def infer_group(key_text: str, code_text: str = "") -> str | None:
        k = (key_text or "").lower()
        c = (code_text or "").lower()
        if ("246" in k) or c.startswith("h246_"):
            return "海河246分区"
        if ("h11" in k) or c.startswith("h11_") or "11分区" in key_text:
            return "海河11分区"
        if ("h32" in k) or c.startswith("h32_") or "32分区" in key_text:
            return "海河32分区"
        if ("h77" in k) or c.startswith("h77_") or "77分区" in key_text:
            return "海河77分区"
        if ("h9" in k) or c.startswith("h9_") or "9分区" in key_text:
            return "海河9分区"
        return None

    def norm_row(item) -> dict[str, str]:
        if isinstance(item, dict):
            name = _pick_first_text(
                item,
                ["name", "partition_name", "subbasin_name", "region_name", "label", "zone_name", "title", "分区名称"],
            )
            code = _pick_first_text(item, ["code", "partition_code", "zone_code", "id", "分区代码"])
            area = _pick_first_text(item, ["region", "所属区域", "area", "group_name"])
            return {"name": name, "code": code, "area": area}
        txt = str(item).strip()
        return {"name": txt, "code": "", "area": ""}

    def walk(obj, parent_key: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k))
            return
        if isinstance(obj, list):
            parsed_rows = [norm_row(it) for it in obj]
            for r in parsed_rows:
                group = infer_group(parent_key, r.get("code", ""))
                if group:
                    groups[group].append(r)
            for it in obj:
                walk(it, parent_key)

    walk(report)

    # 去重
    for gname in list(groups.keys()):
        seen = set()
        deduped = []
        for row in groups[gname]:
            key = (row.get("name", ""), row.get("code", ""), row.get("area", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        groups[gname] = deduped

    return groups


IMPACT_TIME_TOOL_RESULT_KEY = "__impact_time_tool_result__"


async def _enrich_with_impact_time_tool(observation, tool_args, tools, step=None):
    """
    额外调用“河流影响时间估算”工具，把结果挂载到当前 observation，
    供后续结构化报表读取。
    """
    river_name = ""
    if isinstance(tool_args, dict):
        for k in ["river_name", "source_river", "start_river", "river"]:
            v = tool_args.get(k)
            if isinstance(v, str) and v.strip():
                river_name = v.strip()
                break

    obs_data = _unwrap_tool_result(observation)
    if (not river_name) and isinstance(obs_data, dict):
        river_name = (
            _pick_first_text(obs_data, ["river_name", "name", "source_river"])
            or _pick_first_text(obs_data.get("self_report", {}) if isinstance(obs_data.get("self_report"), dict) else {}, ["river_name", "name"])
        )
    if not river_name:
        return observation

    impact_time_tool = None
    for t in tools:
        name = str(getattr(t, "name", ""))
        low = name.lower()
        if name == "estimate_river_impact_time":
            impact_time_tool = t
            break
        if "影响时间" in name and "河" in name:
            impact_time_tool = t
            break
        if ("river" in low and "impact" in low and "time" in low):
            impact_time_tool = t
            break

    if impact_time_tool is None:
        return observation

    trial_args = [
        {"river_name": river_name, "max_rivers": 5},
        {"source_river": river_name, "max_rivers": 5},
        {"start_river": river_name, "max_rivers": 5},
        {"river": river_name, "max_rivers": 5},
        {"river_name": river_name},
    ]

    impact_result = None
    for idx, args in enumerate(trial_args, 1):
        try:
            impact_result = await impact_time_tool.ainvoke(args)
            if step is not None:
                step.input += f"⏱️ 已调用 `{impact_time_tool.name}` 获取下游传导时间（尝试 {idx}）。\n"
            print(f"[影响时间工具] {impact_time_tool.name} 调用成功，args={args}")
            break
        except Exception as e:
            print(f"[影响时间工具] {impact_time_tool.name} 调用失败（尝试 {idx}）：{e}")
            continue

    if impact_result is None:
        return observation

    impact_data = _unwrap_tool_result(impact_result)
    if isinstance(obs_data, dict):
        merged = dict(obs_data)
        merged[IMPACT_TIME_TOOL_RESULT_KEY] = impact_data
        return merged
    return {
        "base_observation": obs_data,
        IMPACT_TIME_TOOL_RESULT_KEY: impact_data,
    }


def _extract_impact_time_window(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    report = data.get("self_report") if isinstance(data, dict) and isinstance(data.get("self_report"), dict) else (data if isinstance(data, dict) else {})

    direct_time = _pick_first_text(
        report,
        [
            "impact_time_window", "time_window", "time_range", "forecast_time_range",
            "valid_time", "valid_period", "有效时间", "时间范围",
        ],
    )
    if direct_time:
        return direct_time

    start_time = _pick_first_text(
        report,
        [
            "start_time", "begin_time", "from_time", "forecast_start_time",
            "valid_start_time", "起始时间", "开始时间",
        ],
    )
    end_time = _pick_first_text(
        report,
        [
            "end_time", "finish_time", "to_time", "forecast_end_time",
            "valid_end_time", "截止时间", "结束时间",
        ],
    )
    if start_time and end_time:
        return f"{start_time} ~ {end_time}"

    # 兜底：从顶层再找一次
    if isinstance(data, dict):
        top_start = _pick_first_text(data, ["start_time", "begin_time", "forecast_start_time", "开始时间"])
        top_end = _pick_first_text(data, ["end_time", "finish_time", "forecast_end_time", "结束时间"])
        if top_start and top_end:
            return f"{top_start} ~ {top_end}"
        top_time = _pick_first_text(data, ["time_range", "impact_time_window", "时间范围"])
        if top_time:
            return top_time

    return "待核实"


def _extract_downstream_transport_time(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    if not isinstance(data, dict):
        return "待核实"
    impact_data = data.get(IMPACT_TIME_TOOL_RESULT_KEY)
    if impact_data is None:
        return "待核实"
    impact_data = _unwrap_tool_result(impact_data)
    if not isinstance(impact_data, dict):
        return "待核实"

    downstream = impact_data.get("downstream")
    if not isinstance(downstream, list) or not downstream:
        return "待核实"

    parts = []
    for row in downstream[:3]:
        if not isinstance(row, dict):
            continue
        river = str(row.get("downstream_river") or row.get("name") or "下游河流").strip()
        avg = row.get("time_estimates", {}).get("avg", {}) if isinstance(row.get("time_estimates"), dict) else {}
        duration = avg.get("duration", {}) if isinstance(avg, dict) else {}
        human = ""
        if isinstance(duration, dict):
            human = str(duration.get("human") or "").strip()
            if not human:
                try:
                    hours = float(duration.get("hours"))
                    human = f"{hours:.2f} 小时"
                except Exception:
                    human = ""
        if human:
            parts.append(f"{river}:{human}")

    if not parts:
        return "待核实"
    return "；".join(parts)


def _build_structured_impact_reply(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    river_name, admin_rows = _extract_admin_unit_rows(raw_result)
    groups = _extract_partition_groups(raw_result)

    report = data.get("self_report") if isinstance(data, dict) and isinstance(data.get("self_report"), dict) else (data if isinstance(data, dict) else {})
    downstream_count = _pick_first_text(report, ["downstream_affected_river_count", "downstream_count", "affected_downstream_river_count"])
    if not downstream_count:
        downstream_count = _pick_first_text(data if isinstance(data, dict) else {}, ["downstream_affected_river_count", "downstream_count"])
    downstream_count = downstream_count or "待核实"
    impact_time_window = _extract_impact_time_window(raw_result)
    transport_time = _extract_downstream_transport_time(raw_result)

    # 概览表
    total_partitions = sum(len(v) for v in groups.values())
    unique_districts = len(
        {
            (r.get("province", ""), r.get("city", ""), r.get("district", ""))
            for r in admin_rows
            if r.get("district")
        }
    )
    lines = [
        f"`{river_name}` 暴雨影响结构化简报（工具结果直出）：",
        "",
        "| 指标 | 数值 | 说明 |",
        "| :--- | :--- | :--- |",
        f"| 影响时间 | {impact_time_window} | 工具返回的时间窗口 |",
        f"| 乡镇/街道数量 | {len(admin_rows)} | 行政区划明细总数 |",
        f"| 区县数量 | {unique_districts or '待核实'} | 去重后的区县层级数量 |",
        f"| 影响分区条目数 | {total_partitions} | 246/11/32/77/9 分区汇总条目 |",
        f"| 下游受影响河流数 | {downstream_count} | 工具返回统计值 |",
        f"| 下游传导时间（平均） | {transport_time} | 由河流影响时间工具估算（距离/流速） |",
        "",
    ]

    # 行政区划表（沿用现有合并逻辑）
    admin_table_text = _build_admin_units_only_reply(raw_result) or ""
    if admin_table_text:
        lines.append("### 行政区划影响表")
        lines.append(admin_table_text)
        lines.append("")

    # 河流影响分区表（新增）
    lines.extend(
        [
            "### 河流影响分区表",
            "| 分区体系 | 条目数 | 代表项（最多3项） |",
            "| :--- | :--- | :--- |",
        ]
    )
    ordered_group_names = ["海河246分区", "海河11分区", "海河32分区", "海河77分区", "海河9分区"]
    for gname in ordered_group_names:
        rows = groups.get(gname, [])
        sample = []
        for row in rows[:3]:
            name = (row.get("name") or row.get("area") or "").strip()
            code = (row.get("code") or "").strip()
            if name and code:
                sample.append(f"{name}({code})")
            elif name:
                sample.append(name)
            elif code:
                sample.append(code)
        sample_text = "、".join(sample) if sample else "—"
        lines.append(f"| {gname} | {len(rows)} | {sample_text} |")

    lines.append("")
    lines.append("如果需要，我可以继续按任一分区体系（如 77 分区）输出完整明细表。")
    return "\n".join(lines)


def _build_partition_only_reply(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    river_name = str(data.get("river_name") or data.get("name") or "该河流").strip() if isinstance(data, dict) else "该河流"
    groups = _extract_partition_groups(raw_result)
    total_partitions = sum(len(v) for v in groups.values())
    impact_time_window = _extract_impact_time_window(raw_result)
    transport_time = _extract_downstream_transport_time(raw_result)

    lines = [
        f"`{river_name}` 暴雨影响分区表（工具结果直出）：",
        "",
        f"影响时间：{impact_time_window}",
        f"下游传导时间（平均）：{transport_time}",
        "",
        "| 分区体系 | 条目数 | 代表项（最多3项） |",
        "| :--- | :--- | :--- |",
    ]
    ordered_group_names = ["海河246分区", "海河11分区", "海河32分区", "海河77分区", "海河9分区"]
    for gname in ordered_group_names:
        rows = groups.get(gname, [])
        sample = []
        for row in rows[:3]:
            name = (row.get("name") or row.get("area") or "").strip()
            code = (row.get("code") or "").strip()
            if name and code:
                sample.append(f"{name}({code})")
            elif name:
                sample.append(name)
            elif code:
                sample.append(code)
        sample_text = "、".join(sample) if sample else "—"
        lines.append(f"| {gname} | {len(rows)} | {sample_text} |")

    lines.extend(
        [
            "",
            f"分区条目总数：{total_partitions}",
            "如需，我可以继续输出某一分区体系的完整明细（名称+代码全量）。",
        ]
    )
    return "\n".join(lines)


async def load_sse_tools():
    client = MultiServerMCPClient(
        {
            "weather": {
                "transport": "sse",
                "url": "http://localhost:3333/sse",
            }
        }
    )
    try:
        tools = await client.get_tools()
        return tools
    except BaseException as e:
        # 后端 MCP SSE 服务异常（包括 ExceptionGroup），打印错误并退化为无工具模式
        print("加载 MCP 工具失败：", repr(e))
        return []


def _unwrap_tool_result(raw_result):
    """把 MCP tool 返回结果统一拆成 Python 对象"""
    if raw_result is None:
        return None

    if isinstance(raw_result, list) and len(raw_result) > 0 and isinstance(raw_result[0], dict) and "text" in raw_result[0]:
        text = raw_result[0]["text"]
        try:
            return json.loads(text)
        except Exception:
            return text

    if isinstance(raw_result, str):
        try:
            return json.loads(raw_result)
        except Exception:
            return raw_result

    if hasattr(raw_result, "content"):
        content = raw_result.content
        if isinstance(content, str):
            try:
                return json.loads(content)
            except Exception:
                return content
        return content

    return raw_result


def _unwrap_scenario_http_payload(data):
    """
    展开 MCP 工具返回的应急 HTTP 响应外壳：{ "success": true, "data": { map_geojson, tables, meta, scenario } }
    """
    if not isinstance(data, dict):
        return data
    inner = data.get("data")
    if data.get("success") is True and isinstance(inner, dict):
        if any(k in inner for k in ("map_geojson", "scenario", "tables", "meta")):
            return inner
    return data


def _http_scenario_to_normalize_scene(http_scenario: str) -> str:
    """将 emergency_http_server 的 data.scenario 映射到 _normalize_scene_export_payload 使用的 scene。"""
    m = {
        "river_downstream": "river_downstream",
        "region_rivers": "region_rivers",
        "emergency_rivers": "emergency_rivers",
        "emergency_admin_regions": "emergency_districts",
        "emergency_regions": "emergency_districts",
        "emergency_partitions": "emergency_partitions",
    }
    return m.get((http_scenario or "").strip(), "emergency_districts")


def _calc_bbox(xs, ys, pad_ratio=0.08, min_pad=0.05):
    if not xs or not ys:
        return None

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    dx = max_x - min_x
    dy = max_y - min_y

    pad_x = max(dx * pad_ratio, min_pad)
    pad_y = max(dy * pad_ratio, min_pad)

    return {
        "min_x": min_x - pad_x,
        "max_x": max_x + pad_x,
        "min_y": min_y - pad_y,
        "max_y": max_y + pad_y,
    }


def _kwargs_for_admin_division_plot(bbox: dict) -> dict:
    """
    只传视域与缓冲；市/县聚合级别与简化参数由 MCP 工具 get_admin_division_for_plot 按跨度自动决定。
    """
    span = max(bbox["max_x"] - bbox["min_x"], bbox["max_y"] - bbox["min_y"])
    if span >= 1.35:
        buf = 0.18
    elif span >= 0.38:
        buf = 0.12
    else:
        buf = 0.06
    return {
        "min_x": bbox["min_x"],
        "min_y": bbox["min_y"],
        "max_x": bbox["max_x"],
        "max_y": bbox["max_y"],
        "buffer_deg": buf,
    }


async def render_and_send_plot(raw_result, title_suffix="全流域", admin_raw_result=None):
    """画行政区划底图 + 河网图 + 河流名称"""
    from collections import defaultdict

    segments = _unwrap_tool_result(raw_result)
    admin_features = _unwrap_tool_result(admin_raw_result) if admin_raw_result is not None else []

    if not segments or not isinstance(segments, list):
        await cl.Message(content=f"暂未获取到可展示的河系数据（{title_suffix}），请稍后重试。").send()
        return

    in_degree = defaultdict(int)
    out_degree = defaultdict(int)
    valid_segments = []
    all_x = []
    all_y = []

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            order = int(seg.get("strahler_order", 1))
            r_name = seg.get("rivername", "")
            line_list = []

            geom = seg.get("geometry")
            if isinstance(geom, dict):
                gtype = geom.get("type")
                coords = geom.get("coordinates")
                if gtype == "LineString" and isinstance(coords, list) and len(coords) >= 2:
                    line_list.append(coords)
                elif gtype == "MultiLineString" and isinstance(coords, list):
                    line_list.extend([ln for ln in coords if isinstance(ln, list) and len(ln) >= 2])

            if not line_list and isinstance(seg.get("paths"), list):
                line_list.extend([ln for ln in seg.get("paths", []) if isinstance(ln, list) and len(ln) >= 2])
            if not line_list and isinstance(seg.get("path"), list) and len(seg.get("path")) >= 2:
                line_list.append(seg.get("path"))
            if not line_list:
                x1, y1 = float(seg["from_x"]), float(seg["from_y"])
                x2, y2 = float(seg["to_x"]), float(seg["to_y"])
                line_list = [[[x1, y1], [x2, y2]]]

            for ln in line_list:
                xy = [(float(p[0]), float(p[1])) for p in ln if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(xy) < 2:
                    continue
                start_node, end_node = xy[0], xy[-1]
                out_degree[start_node] += 1
                in_degree[end_node] += 1
                valid_segments.append({"coords": xy, "order": order, "rivername": r_name})
                all_x.extend([p[0] for p in xy])
                all_y.extend([p[1] for p in xy])
        except Exception:
            continue

    if not valid_segments:
        await cl.Message(content=f"当前河系数据为空（{title_suffix}），建议更换时间或范围后重试。").send()
        return

    bbox = _calc_bbox(all_x, all_y)

    fig, ax = plt.subplots(figsize=(12, 10))

    # 0. 先画行政区划底图
    if isinstance(admin_features, list):
        for feat in admin_features:
            if not isinstance(feat, dict):
                continue

            polygons = feat.get("polygons", [])
            feat_name = feat.get("name", "")
            label_drawn = False

            for poly in polygons:
                if not isinstance(poly, dict):
                    continue

                outer = poly.get("outer", [])
                holes = poly.get("holes", [])

                if len(outer) < 3:
                    continue

                xs = [p[0] for p in outer]
                ys = [p[1] for p in outer]

                ax.fill(
                    xs, ys,
                    facecolor="#F2F2F2",
                    edgecolor="#B5B5B5",
                    linewidth=0.8,
                    alpha=0.65,
                    zorder=0
                )

                # 内洞简单抠白
                for hole in holes:
                    if len(hole) < 3:
                        continue
                    hx = [p[0] for p in hole]
                    hy = [p[1] for p in hole]
                    ax.fill(
                        hx, hy,
                        facecolor="white",
                        edgecolor="#D5D5D5",
                        linewidth=0.4,
                        alpha=1.0,
                        zorder=0.1
                    )

                # 每个行政区只打一次轻量标签
                if (not label_drawn) and feat_name:
                    use_x = xs[:-1] if len(xs) > 1 else xs
                    use_y = ys[:-1] if len(ys) > 1 else ys
                    if use_x and use_y:
                        cx = sum(use_x) / len(use_x)
                        cy = sum(use_y) / len(use_y)
                        if bbox and (bbox["min_x"] <= cx <= bbox["max_x"]) and (bbox["min_y"] <= cy <= bbox["max_y"]):
                            ax.text(
                                cx, cy, feat_name,
                                fontsize=8,
                                color="#666666",
                                ha="center",
                                va="center",
                                zorder=0.2,
                                clip_on=True,   # 关键：超出坐标轴就裁掉
                                bbox=dict(facecolor="white", alpha=0.45, edgecolor="none", boxstyle="round,pad=0.15")
                            )
                        label_drawn = True

    # 1. 再画河道与水流方向
    for seg in valid_segments:
        coords = seg.get("coords") or []
        if len(coords) < 2:
            continue
        x1, y1 = coords[0]
        x2, y2 = coords[-1]
        lw = 0.8 + (seg["order"] * 1.5)

        ax.plot(
            [p[0] for p in coords], [p[1] for p in coords],
            color="#8CB4E2",
            linewidth=lw,
            alpha=0.95,
            solid_capstyle='round',
            zorder=1
        )

        mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
        dx, dy = (x2 - x1), (y2 - y1)
        ax.annotate(
            '',
            xy=(mid_x + dx * 0.01, mid_y + dy * 0.01),
            xytext=(mid_x, mid_y),
            arrowprops=dict(arrowstyle="->", color="#1F4E79", shrinkA=0, shrinkB=0, lw=1.0),
            zorder=2
        )

    # 2. 画交汇节点
    all_nodes = set(in_degree.keys()).union(set(out_degree.keys()))
    for node in all_nodes:
        in_d, out_d = in_degree[node], out_degree[node]
        color, size = "blue", 30
        if in_d == 0:
            color = "green"
        elif out_d == 0:
            color, size = "red", 60
        elif in_d > 1:
            color = "orange"

        ax.scatter(
            node[0], node[1],
            color=color,
            s=size,
            zorder=3,
            edgecolors='white',
            linewidths=0.5
        )

    # 3. 河流名称标注
    river_label_coords = defaultdict(list)
    for seg in valid_segments:
        name = seg["rivername"]
        if name and name not in ["未知", "None", ""]:
            coords = seg.get("coords") or []
            if len(coords) < 2:
                continue
            mid_pt = coords[len(coords) // 2]
            mid_x, mid_y = mid_pt[0], mid_pt[1]
            river_label_coords[name].append((mid_x, mid_y))

    for name, coords in river_label_coords.items():
        mid_index = len(coords) // 2
        pos_x, pos_y = coords[mid_index]
        ax.text(
            pos_x, pos_y, name,
            fontsize=10,
            color='darkred',
            fontweight='bold',
            ha='center',
            va='center',
            zorder=4,
            bbox=dict(facecolor='white', alpha=0.75, edgecolor='none', boxstyle='round,pad=0.2')
        )

    title = f"海河流域水系拓扑图- {title_suffix}"
    ax.set_title(title)
    ax.set_xlabel("经度")
    ax.set_ylabel("纬度")
    ax.grid(True, linestyle="--", alpha=0.35, color='#CCCCCC')
    ax.set_facecolor('#FAFAFA')
    ax.set_aspect('equal', adjustable='box')

    if bbox:
        ax.set_xlim(bbox["min_x"], bbox["max_x"])
        ax.set_ylim(bbox["min_y"], bbox["max_y"])

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=200)
    plt.close(fig)
    buf.seek(0)

    await cl.Message(
        content=f"📊 已生成【{title_suffix}】暴雨的影响范围图：",
        elements=[cl.Image(content=buf.getvalue(), name="river_network_with_admin")],
    ).send()


def _build_messages_from_thread(thread: ThreadDict | dict | None):
    """
    将历史线程的 step 记录恢复为 LangChain 消息序列，支持续聊上下文。
    """
    if not thread or not isinstance(thread, dict):
        return []

    resumed_messages = []
    steps = thread.get("steps", [])
    if not isinstance(steps, list):
        return resumed_messages

    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = str(step.get("type") or "").strip()
        output_text = str(step.get("output") or "").strip()
        input_text = str(step.get("input") or "").strip()

        if step_type == "user_message":
            text = output_text or input_text
            if text:
                resumed_messages.append(HumanMessage(content=text))
        elif step_type == "assistant_message":
            if output_text:
                resumed_messages.append(AIMessage(content=output_text))

    return resumed_messages


async def _init_runtime_session(messages_seed=None):
    """
    初始化会话运行时对象；用于首聊和历史线程恢复。
    """
    await _ensure_chainlit_tables()

    planner_llm = ChatTongyi(
        model="qwen-plus",
        streaming=False,
        temperature=0.7,
        api_key="sk-40c16d460ec44feb91006524c12ad8b2"
    )
    answer_llm = ChatTongyi(
        model="qwen-plus",
        streaming=False,
        temperature=0.7,
        api_key="sk-40c16d460ec44feb91006524c12ad8b2"
    )

    tools = await load_sse_tools()
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", WEATHER_ASSISTANT_PROMPT),
        MessagesPlaceholder(variable_name="messages"),
    ])
    planner_chain = prompt_template | planner_llm.bind_tools(tools)
    answer_chain = prompt_template | answer_llm

    cl.user_session.set("planner_chain", planner_chain)
    cl.user_session.set("answer_chain", answer_chain)
    cl.user_session.set("tools", tools)
    cl.user_session.set("messages", messages_seed if isinstance(messages_seed, list) else [])


def _need_river_plot(user_text: str) -> bool:
    """识别用户是否在问河网/水系结构可视化"""
    if not user_text:
        return False
    text = user_text.strip()
    plot_keywords = ["河网", "河道图", "水系图", "拓扑图", "流域图", "路径图", "示意图", "分布图"]
    verb_keywords = ["画", "绘", "生成", "展示", "看", "给出", "出一下", "可视化", "图"]
    has_plot_topic = any(k in text for k in plot_keywords)
    has_visual_intent = any(k in text for k in verb_keywords)
    return has_plot_topic or ("河" in text and has_visual_intent)


def _extract_river_name(user_text: str) -> str:
    """从用户问题里尽量提取“某某河”，提取不到就用全流域"""
    if not user_text:
        return "全流域"

    normalized = re.sub(r"[\s，。！？、,.!?：:；;（）()]", "", user_text)
    action_prefix_pattern = (
        r"(帮我|请|给我|给|麻烦|帮忙|我想|想看|看下|看一下|查看|展示|生成|绘制|画一下|画出|画个|画|一下|把)"
    )

    # 优先：在“河网/拓扑图/水系图”语境中抓取目标河名
    scene_match = re.search(
        r"([\u4e00-\u9fa5]{1,20}?)(?:的)?(?:河网|河道图|水系图|拓扑图|流域图|路径图|示意图|分布图)",
        normalized,
    )
    if scene_match:
        prefix = scene_match.group(1)
        prefix = re.sub(action_prefix_pattern, "", prefix)
        prefix = prefix.rstrip("的")
        tail_match = re.search(r"([\u4e00-\u9fa5]{1,8}河)$", prefix)
        if tail_match:
            return tail_match.group(1)

    # 兜底：遍历“xx河”候选，剔除口语前缀、结构助词污染
    candidates = []
    for m in re.finditer(r"([\u4e00-\u9fa5]{1,12}河)", normalized):
        cand = m.group(1)
        cand = re.sub(rf"^(?:{action_prefix_pattern})+", "", cand)
        if "的" in cand:
            cand = cand.split("的")[0]
        cand = cand.strip()
        if re.fullmatch(r"[\u4e00-\u9fa5]{1,8}河", cand):
            candidates.append(cand)

    if candidates:
        return max(candidates, key=len)

    return "全流域"


async def _build_admin_overlay_for_plot(tools, river_observation):
    """根据河网结果自动计算 bbox 并拉取行政区划底图。"""
    admin_tool = next((t for t in tools if t.name == "get_admin_division_for_plot"), None)
    if not admin_tool:
        return None

    seg_list = _unwrap_tool_result(river_observation)
    xs, ys = [], []
    if isinstance(seg_list, list):
        for seg in seg_list:
            if not isinstance(seg, dict):
                continue
            try:
                geom = seg.get("geometry")
                if isinstance(geom, dict):
                    gtype = geom.get("type")
                    coords = geom.get("coordinates")
                    if gtype == "LineString" and isinstance(coords, list):
                        for p in coords:
                            if isinstance(p, (list, tuple)) and len(p) >= 2:
                                xs.append(float(p[0]))
                                ys.append(float(p[1]))
                    elif gtype == "MultiLineString" and isinstance(coords, list):
                        for ln in coords:
                            if not isinstance(ln, list):
                                continue
                            for p in ln:
                                if isinstance(p, (list, tuple)) and len(p) >= 2:
                                    xs.append(float(p[0]))
                                    ys.append(float(p[1]))
                elif isinstance(seg.get("paths"), list):
                    for ln in seg.get("paths", []):
                        if not isinstance(ln, list):
                            continue
                        for p in ln:
                            if isinstance(p, (list, tuple)) and len(p) >= 2:
                                xs.append(float(p[0]))
                                ys.append(float(p[1]))
                elif isinstance(seg.get("path"), list):
                    for p in seg.get("path", []):
                        if isinstance(p, (list, tuple)) and len(p) >= 2:
                            xs.append(float(p[0]))
                            ys.append(float(p[1]))
                else:
                    xs.extend([float(seg["from_x"]), float(seg["to_x"])])
                    ys.extend([float(seg["from_y"]), float(seg["to_y"])])
            except Exception:
                continue

    bbox = _calc_bbox(xs, ys)
    if not bbox:
        return None

    return await admin_tool.ainvoke(_kwargs_for_admin_division_plot(bbox))


def _tool_observation_to_text(observation):
    if isinstance(observation, list) and observation and isinstance(observation[0], dict) and "text" in observation[0]:
        return observation[0].get("text", str(observation))
    if isinstance(observation, dict):
        return observation.get("text", str(observation))
    return str(observation)


def _guess_gis_scene(user_text: str) -> str:
    text = (user_text or "").strip()
    if "下流" in text and "河" in text:
        return "river_downstream"
    if ("分区" in text or "行政区" in text) and "河" in text:
        return "district_rivers"
    # 兼容“天津市有哪些河流”“海淀区有哪些河流”这类口语问法
    if ("哪些河流" in text or "有什么河流" in text or "河流有哪些" in text) and any(k in text for k in ["市", "区", "县"]):
        return "district_rivers"
    if ("站点" in text or "国家站" in text) and ("观测" in text or "气象" in text):
        return "realtime_station"
    if (
        ("应急" in text and "河" in text)
        or ("预报影响" in text and any(k in text for k in ("河流", "河系", "河道", "河")))
        or ("受影响河流" in text)
        or ("受影响河系" in text)
        or ("可能影响的河流" in text)
    ):
        return "emergency_rivers"
    if "应急" in text and "行政区" in text:
        return "emergency_districts"
    if "应急" in text and "分区" in text:
        return "emergency_partitions"
    return "generic"


def _parse_common_time_to_14(raw) -> str | None:
    """把常见时间表达转成 yyyyMMddHHmmss。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # 1) 纯数字：14 位直通；12 位补秒。
    digits = re.sub(r"\D", "", text)
    if len(digits) == 14:
        return digits
    if len(digits) == 12:
        return f"{digits}00"
    if len(digits) == 8:
        return f"{digits}000000"

    # 1.5) 口语时间：今天/昨天/前天 + (上午/下午/晚上/凌晨) + X点(半)
    # 仅用于把业务表达转为接口需要的 YYYYMMDDHHMMSS（分秒默认 000000 或 300000）
    try:
        now_dt = datetime.now()
        t_simple = re.sub(r"[，,。.!！？?；;、]+", " ", text).strip()
        t_simple = re.sub(r"\s+", " ", t_simple)

        if any(k in t_simple for k in ("今天", "昨日", "昨天", "前天")) and ("点" in t_simple):
            day_offset = 0
            if "前天" in t_simple:
                day_offset = -2
            elif "昨日" in t_simple or "昨天" in t_simple:
                day_offset = -1
            m = re.search(r"(今天|昨日|昨天|前天)\s*(上午|下午|晚上|凌晨)?\s*(\d{1,2})\s*点\s*(半)?", t_simple)
            if m:
                part = (m.group(2) or "").strip()
                hour = int(m.group(3))
                is_half = bool(m.group(4))
                if part in {"下午", "晚上"} and 1 <= hour <= 11:
                    hour += 12
                if part == "凌晨" and hour == 12:
                    hour = 0
                base = (now_dt.replace(minute=0, second=0, microsecond=0) + timedelta(days=day_offset)).date()
                dt = datetime(base.year, base.month, base.day, max(0, min(23, hour)), 30 if is_half else 0, 0)
                return dt.strftime("%Y%m%d%H%M%S")

        # 相对时间：现在/当前/刚才（取当前小时整点）
        if any(k in t_simple for k in ("现在", "当前", "刚才")):
            dt = now_dt.replace(minute=0, second=0, microsecond=0)
            return dt.strftime("%Y%m%d%H%M%S")
    except Exception:
        pass

    # 2) 中文/口语：2023年7月29号14点(30分/15秒)
    m_cn = re.search(
        r"(?P<y>\d{4})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]?\s*"
        r"(?:(?P<h>\d{1,2})\s*[点时](?:(?P<mi>\d{1,2})\s*分?)?(?:(?P<s>\d{1,2})\s*秒?)?)?",
        text,
    )
    if m_cn:
        try:
            y = int(m_cn.group("y"))
            mo = int(m_cn.group("mo"))
            d = int(m_cn.group("d"))
            h = int(m_cn.group("h") or 0)
            mi = int(m_cn.group("mi") or 0)
            s = int(m_cn.group("s") or 0)
            return datetime(y, mo, d, h, mi, s).strftime("%Y%m%d%H%M%S")
        except Exception:
            pass

    # 3) 横杠/斜杠：2023-07-29 14:00[:00]、2023/7/29 14:00
    m_std = re.search(
        r"(?P<y>\d{4})[-/](?P<mo>\d{1,2})[-/](?P<d>\d{1,2})"
        r"(?:\s+(?P<h>\d{1,2})(?::(?P<mi>\d{1,2}))?(?::(?P<s>\d{1,2}))?)?",
        text,
    )
    if m_std:
        try:
            y = int(m_std.group("y"))
            mo = int(m_std.group("mo"))
            d = int(m_std.group("d"))
            h = int(m_std.group("h") or 0)
            mi = int(m_std.group("mi") or 0)
            s = int(m_std.group("s") or 0)
            return datetime(y, mo, d, h, mi, s).strftime("%Y%m%d%H%M%S")
        except Exception:
            pass

    # 4) 文本里任意出现 14 位时间串。
    m14 = re.search(r"(\d{14})", text)
    if m14:
        return m14.group(1)
    return None


def _ec_forecast_start_compact_from_times14(times_14: str | None) -> str:
    """从业务 14 位时次得到 EC 起报十位 YYYYMMDDHH，并归一到合法起报时次。"""
    digits = "".join(c for c in str(times_14 or "") if c.isdigit())
    if len(digits) < 10:
        return ""
    return _normalize_forecast_start_time_10(digits[:10])


def _normalize_forecast_start_time_10(compact_10: str) -> str:
    """
    预报产品任务要求起报小时在固定集合内。
    将 YYYYMMDDHH 归一到不晚于当前小时的最近合法值，避免 17 点这类非法时次导致任务失败。
    """
    text = str(compact_10 or "").strip()
    if not re.fullmatch(r"\d{10}", text):
        return text
    dt = datetime.strptime(text, "%Y%m%d%H")
    valid_hours = [0, 2, 6, 8, 12, 14, 18, 20]
    candidates = [h for h in valid_hours if h <= dt.hour]
    target_h = max(candidates) if candidates else valid_hours[-1]
    if not candidates:
        dt = dt - timedelta(days=1)
    dt = dt.replace(hour=target_h)
    return dt.strftime("%Y%m%d%H")


def _load_local_ec_output_path_for_debug() -> str:
    """
    本地调试时，从 MCP 侧 config.ini 读取 ecOutput 目录，避免智能体任务与后端配置不一致。
    """
    default_path = "D:/tj/data"
    try:
        mcp_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "haihe-weather-analyzer-mcp"))
        cfg = os.path.join(mcp_root, "config.ini")
        if not os.path.isfile(cfg):
            return default_path
        cp = configparser.ConfigParser()
        cp.read(cfg, encoding="utf-8")
        if cp.has_section("paths"):
            return cp.get("paths", "ecOutput", fallback=default_path).strip() or default_path
    except Exception:
        pass
    return default_path


def _pick_latest_available_forecast_start_time_10(ec_output_path: str, req_hours: list[int]) -> str | None:
    """
    在本地 ecOutput 目录中，按文件名 ec_YYYYMMDDHH_rain_total_{N}h.tif 选择最近可用起报时次。
    仅当该时次包含 req_hours 中所有时效时才算可用。
    """
    if not ec_output_path or not os.path.isdir(ec_output_path):
        return None
    need = {int(h) for h in (req_hours or [])}
    patt = re.compile(r"^ec_(\d{10})_rain_total_(\d+)h\.tif$", re.IGNORECASE)
    bucket: dict[str, set[int]] = {}
    try:
        for root, _, files in os.walk(ec_output_path):
            for fn in files:
                m = patt.match(fn)
                if not m:
                    continue
                cyc = m.group(1)
                h = int(m.group(2))
                bucket.setdefault(cyc, set()).add(h)
    except Exception:
        return None
    if not bucket:
        return None
    candidates = []
    for cyc, hs in bucket.items():
        if need and not need.issubset(hs):
            continue
        candidates.append(cyc)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


def _find_local_scene_slot(slots, tool_args, user_text: str):
    if not isinstance(slots, list) or not slots:
        return None

    target_times = None
    if isinstance(tool_args, dict):
        for k in ("times", "time", "timestamp", "dt", "datetime"):
            v = tool_args.get(k)
            parsed = _parse_common_time_to_14(v)
            if parsed:
                target_times = parsed
                break
    if not target_times:
        target_times = _parse_common_time_to_14(user_text)

    if target_times:
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            if str(slot.get("times") or "").strip() == target_times:
                return slot

    for slot in slots:
        if isinstance(slot, dict) and slot.get("ok") is True:
            return slot
    return next((s for s in slots if isinstance(s, dict)), None)


def _normalize_scene_export_payload(scene: str, payload: dict):
    if not isinstance(payload, dict):
        return None

    map_geojson = payload.get("map_geojson")
    if isinstance(map_geojson, str):
        try:
            map_geojson = json.loads(map_geojson)
        except Exception:
            map_geojson = {"type": "FeatureCollection", "features": []}
    if not isinstance(map_geojson, dict):
        map_geojson = {"type": "FeatureCollection", "features": []}

    tables = []
    for t in payload.get("tables", []):
        if not isinstance(t, dict):
            continue
        cols = []
        for c in t.get("columns", []):
            if not isinstance(c, dict):
                continue
            key = str(c.get("key") or "").strip()
            if not key:
                continue
            cols.append({"key": key, "label": c.get("title") or c.get("label") or key})

        rows = []
        for r in t.get("rows", []):
            if not isinstance(r, dict):
                continue
            rr = dict(r)
            # 统一联动字段，保障点击表格可定位。
            if rr.get("locate_id") is None:
                rr["locate_id"] = rr.get("feature_id") or rr.get("id") or rr.get("station_id")
            if scene == "realtime_station":
                # 兼容本地样例字段命名
                if rr.get("wind") is None and rr.get("wind_speed") is not None:
                    rr["wind"] = rr.get("wind_speed")
            rows.append(rr)

        table_id = str(t.get("table_key") or t.get("id") or "table")
        title = str(t.get("table_name") or t.get("title") or table_id)
        tables.append(_build_table(table_id, title, cols, rows))

    if scene == "realtime_station":
        layers = [{"id": "national_stations", "color": "#E67E22", "name": "国家站点"}]
    elif scene == "river_downstream":
        layers = [
            {"id": "current_river", "color": "#2F80ED", "name": "当前河流"},
            {"id": "downstream_rivers", "color": "#27AE60", "name": "下流河系"},
        ]
    elif scene == "region_rivers":
        layers = [
            {"id": "district_boundary", "color": "#95A5A6", "name": "行政区边界"},
            {"id": "river_in_district", "color": "#2F80ED", "name": "区内河流"},
        ]
    elif scene == "emergency_rivers":
        layers = [
            {"id": "direct_rivers", "color": "#27AE60", "name": "直接影响河流"},
            {"id": "indirect_rivers", "color": "#2F80ED", "name": "间接影响河流"},
        ]
    elif scene == "emergency_districts":
        layers = [{"id": "emergency_districts", "color": "#8E44AD", "name": "应急影响行政区"}]
    elif scene == "emergency_partitions":
        layers = [{"id": "emergency_partitions", "color": "#8E44AD", "name": "应急影响分区"}]
    else:
        layers = []

    return {
        "geojson": map_geojson,
        "panel_tables": tables,
        "layers": layers,
    }


def _load_local_scene_export(scene: str, tool_args, user_text: str):
    scene_file_map = {
        "realtime_station": "monitor.json",
        "emergency_rivers": "emergency_rivers.json",
        "emergency_districts": "emergency_admin_regions.json",
        "emergency_partitions": "emergency_partitions.json",
    }
    file_name = scene_file_map.get(scene)
    if not file_name:
        return None

    base_dir = os.getenv("GIS_SCENE_EXPORT_DIR", r"C:\Users\gaozr\Desktop\fsdownload\scene_exports")
    file_path = os.path.join(base_dir, file_name)
    # 兼容旧导出：两个场景都落在 emergency_regions.json
    if not os.path.exists(file_path):
        if scene in {"emergency_districts", "emergency_partitions"}:
            legacy_path = os.path.join(base_dir, "emergency_regions.json")
            if os.path.exists(legacy_path):
                file_path = legacy_path
                file_name = "emergency_regions.json"
            else:
                return None
        else:
            return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            root = json.load(f)
    except Exception as e:
        print(f"[本地场景JSON] 读取失败 {file_path}: {e}")
        return None

    slot = _find_local_scene_slot(root.get("slots"), tool_args, user_text)
    if not isinstance(slot, dict):
        return None

    payload = None
    if scene == "realtime_station":
        payload = ((slot.get("monitor_stations") or {}).get("data"))
    elif scene == "emergency_rivers":
        er = slot.get("emergency_rivers") if isinstance(slot.get("emergency_rivers"), dict) else {}
        payload = er.get("data")
        if not isinstance(payload, dict):
            # 兼容 emergency_rivers 的 items[*].payload.data 结构
            items = er.get("items") if isinstance(er.get("items"), list) else []
            merged_features = []
            tables_by_key = {}

            for it in items:
                if not isinstance(it, dict):
                    continue
                pdata = (((it.get("payload") or {}).get("data")) if isinstance(it.get("payload"), dict) else None)
                if not isinstance(pdata, dict):
                    continue

                # 聚合地图
                mg = pdata.get("map_geojson")
                if isinstance(mg, str):
                    try:
                        mg = json.loads(mg)
                    except Exception:
                        mg = None
                if isinstance(mg, dict) and isinstance(mg.get("features"), list):
                    for f in mg.get("features", []):
                        if isinstance(f, dict):
                            merged_features.append(f)

                # 聚合表
                for t in (pdata.get("tables") if isinstance(pdata.get("tables"), list) else []):
                    if not isinstance(t, dict):
                        continue
                    tkey = str(t.get("table_key") or t.get("id") or "table")
                    if tkey not in tables_by_key:
                        tables_by_key[tkey] = {
                            "table_key": tkey,
                            "table_name": t.get("table_name") or t.get("title") or tkey,
                            "columns": t.get("columns") if isinstance(t.get("columns"), list) else [],
                            "rows": [],
                        }
                    rows = t.get("rows") if isinstance(t.get("rows"), list) else []
                    tables_by_key[tkey]["rows"].extend([r for r in rows if isinstance(r, dict)])

            if merged_features or tables_by_key:
                payload = {
                    "scenario": "emergency_rivers",
                    "map_geojson": {"type": "FeatureCollection", "features": merged_features},
                    "tables": list(tables_by_key.values()),
                }
    elif scene == "emergency_districts":
        payload = (
            ((slot.get("emergency_admin_regions") or {}).get("data"))
            or ((slot.get("emergency_regions") or {}).get("data"))
        )
    elif scene == "emergency_partitions":
        payload = (
            ((slot.get("emergency_partitions") or {}).get("data"))
            or ((slot.get("emergency_regions") or {}).get("data"))
        )

    normalized = _normalize_scene_export_payload(scene, payload)
    if normalized:
        normalized["_local_meta"] = {
            "data_origin": "local_scene_export",
            "source_file": file_name,
            "source_path": file_path,
            "slot_times": str(slot.get("times") or ""),
        }
        print(f"[本地场景JSON] 已加载 {file_name} times={slot.get('times')}")
    return normalized


def _segments_to_geojson(segments):
    def _iter_segment_lines(seg: dict):
        if not isinstance(seg, dict):
            return

        geom = seg.get("geometry")
        if isinstance(geom, dict):
            gtype = geom.get("type")
            coords = geom.get("coordinates")
            if gtype == "LineString" and isinstance(coords, list) and len(coords) >= 2:
                yield coords
                return
            if gtype == "MultiLineString" and isinstance(coords, list):
                emitted = False
                for ln in coords:
                    if isinstance(ln, list) and len(ln) >= 2:
                        yield ln
                        emitted = True
                if emitted:
                    return

        paths = seg.get("paths")
        if isinstance(paths, list):
            emitted = False
            for ln in paths:
                if isinstance(ln, list) and len(ln) >= 2:
                    yield ln
                    emitted = True
            if emitted:
                return

        path = seg.get("path")
        if isinstance(path, list) and len(path) >= 2:
            yield path
            return

        try:
            x1, y1 = float(seg["from_x"]), float(seg["from_y"])
            x2, y2 = float(seg["to_x"]), float(seg["to_y"])
            yield [[x1, y1], [x2, y2]]
        except Exception:
            return

    features = []
    if not isinstance(segments, list):
        return {"type": "FeatureCollection", "features": []}

    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue

        locate_id = seg.get("id") or seg.get("segment_id") or f"river_seg_{i+1}"
        lines = list(_iter_segment_lines(seg))
        if not lines:
            continue
        geometry = (
            {"type": "LineString", "coordinates": lines[0]}
            if len(lines) == 1
            else {"type": "MultiLineString", "coordinates": lines}
        )
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "locate_id": str(locate_id),
                    "river_name": seg.get("rivername", ""),
                    "length_km": seg.get("length_km"),
                    # 业务口径：距离列默认展示该河段长度
                    "distance_km": seg.get("distance") or seg.get("length_km") or seg.get("length"),
                    "strahler_order": seg.get("strahler_order"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _station_rows_to_geojson(rows):
    features = []
    if not isinstance(rows, list):
        return {"type": "FeatureCollection", "features": []}

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        lon = row.get("lon", row.get("lng", row.get("longitude")))
        lat = row.get("lat", row.get("latitude"))
        if lon is None or lat is None:
            continue
        try:
            lon = float(lon)
            lat = float(lat)
        except Exception:
            continue

        locate_id = (
            row.get("locate_id")
            or row.get("station_id")
            or row.get("stcd")
            or row.get("id")
            or f"station_{i+1}"
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "locate_id": str(locate_id),
                    "station_name": row.get("station_name") or row.get("name") or "",
                    "temperature": row.get("temperature"),
                    "humidity": row.get("humidity"),
                    "wind": row.get("wind"),
                    "pressure": row.get("pressure"),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def _extract_station_rows(data):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []

    candidate_keys = ["stations", "station_list", "station_data", "obs_list", "records", "items"]
    for key in candidate_keys:
        val = data.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    return []


def _extract_emergency_tables(data):
    if not isinstance(data, dict):
        return []

    # 兼容你给的 basin_judgment 实例（slots[].judgment.evidence.checks）
    slots = data.get("slots")
    if isinstance(slots, list) and slots:
        latest = slots[-1] if isinstance(slots[-1], dict) else {}
        judgment = latest.get("judgment") if isinstance(latest.get("judgment"), dict) else {}
        evidence = judgment.get("evidence") if isinstance(judgment.get("evidence"), dict) else {}
        checks = evidence.get("checks") if isinstance(evidence.get("checks"), list) else []
        check_rows = []
        for c in checks:
            if not isinstance(c, dict):
                continue
            e = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
            check_rows.append(
                {
                    "candidate_level": c.get("candidate_level"),
                    "triggered": c.get("triggered"),
                    "window_hours": e.get("window_hours"),
                    "rain_label": e.get("rain_label"),
                    "threshold_mm": e.get("threshold_mm"),
                    "qualified_station_count": e.get("qualified_station_count"),
                    "qualified_adjacent_station_count": e.get("qualified_adjacent_station_count"),
                    "sustained_station_count": e.get("sustained_station_count"),
                    "ratio": e.get("ratio"),
                    "locate_id": str(c.get("candidate_level") or ""),
                }
            )

        summary_rows = [
            {
                "times": latest.get("times") or latest.get("times_compact"),
                "reached": judgment.get("reached"),
                "level": judgment.get("level"),
                "message": judgment.get("message"),
                "total_station_count": evidence.get("total_station_count"),
                "neighbor_km": evidence.get("neighbor_km"),
                "sustain_hourly_threshold_mm": evidence.get("sustain_hourly_threshold_mm"),
                "locate_id": str(latest.get("times") or ""),
            }
        ]

        return [
            {
                "id": "emergency_summary",
                "title": "应急响应判定",
                "columns": [
                    "times",
                    "reached",
                    "level",
                    "message",
                    "total_station_count",
                    "neighbor_km",
                    "sustain_hourly_threshold_mm",
                    "locate_id",
                ],
                "rows": summary_rows,
            },
            {
                "id": "emergency_level_checks",
                "title": "等级判定明细",
                "columns": [
                    "candidate_level",
                    "triggered",
                    "window_hours",
                    "rain_label",
                    "threshold_mm",
                    "qualified_station_count",
                    "qualified_adjacent_station_count",
                    "sustained_station_count",
                    "ratio",
                    "locate_id",
                ],
                "rows": check_rows,
            },
        ]

    # 兼容河流应急：直接/间接影响河流
    direct = data.get("direct_rivers") if isinstance(data.get("direct_rivers"), list) else []
    indirect = data.get("indirect_rivers") if isinstance(data.get("indirect_rivers"), list) else []
    if direct or indirect:
        return [
            {
                "id": "direct_rivers",
                "title": "直接影响河流",
                "columns": ["name", "length_km", "distance_km", "locate_id"],
                "rows": direct,
            },
            {
                "id": "indirect_rivers",
                "title": "间接影响河流",
                "columns": ["name", "length_km", "distance_km", "locate_id"],
                "rows": indirect,
            },
        ]

    return []


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _format_compact_time_text(v):
    """将 14 位紧凑时间转成 YYYY-MM-DD HH:MM:SS，并支持区间文本内替换。"""
    if v is None:
        return v
    text = str(v)
    if not text:
        return v

    def _repl(m):
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"

    return re.sub(r"(\d{14})", _repl, text)


def _format_table_cell(key: str, value):
    # 河流公里数统一保留两位小数
    if key in {
        "length_km", "distance_km", "river_length_km", "river_distance_km",
        "impact_distance_km", "len_km", "dist_km",
    }:
        fv = _to_float(value)
        if fv is not None:
            return round(fv, 2)

    # 时间字段统一分割展示
    if key in {"times", "time", "timestamp", "dt", "datetime", "time_range", "slot_times"}:
        return _format_compact_time_text(value)

    return value


def _build_table(id_: str, title: str, columns, rows):
    col_keys = [c["key"] for c in columns]
    safe_rows = rows if isinstance(rows, list) else []
    formatted_rows = []
    for r in safe_rows:
        if not isinstance(r, dict):
            continue
        rr = {}
        for k, v in r.items():
            rr[k] = _format_table_cell(str(k), v)
        formatted_rows.append(rr)
    # grid 为按 columns 顺序展开的二维数组，前端可直接用于表格组件渲染
    grid = [
        [r.get(k) if isinstance(r, dict) else None for k in col_keys]
        for r in formatted_rows
    ]
    return {
        "id": id_,
        "title": title,
        "columns": col_keys,
        "column_defs": columns,  # 给前端更清晰的列定义（key + label）
        "rows": formatted_rows,
        "row_count": len(formatted_rows),
        "col_count": len(col_keys),
        "grid": grid,
    }


def _aggregate_river_rows_from_geojson(geojson):
    features = geojson.get("features", []) if isinstance(geojson, dict) else []
    grouped = {}
    for f in features:
        if not isinstance(f, dict):
            continue
        p = f.get("properties", {}) if isinstance(f.get("properties"), dict) else {}
        name = (p.get("river_name") or "未命名河流").strip()
        locate_id = str(p.get("locate_id") or "")
        length = _to_float(p.get("length_km"))
        distance = _to_float(p.get("distance_km"))
        if name not in grouped:
            grouped[name] = {"river_name": name, "length_km": 0.0, "distance_km": None, "locate_id": locate_id}
        if length is not None:
            grouped[name]["length_km"] += length
        if grouped[name]["distance_km"] is None and distance is not None:
            grouped[name]["distance_km"] = distance
        if not grouped[name]["locate_id"] and locate_id:
            grouped[name]["locate_id"] = locate_id

    rows = list(grouped.values())
    # 业务口径统一：距离=河长（聚合后总长度）
    for r in rows:
        r["distance_km"] = r.get("length_km")
    rows.sort(key=lambda r: ((r["distance_km"] is None), r["distance_km"] or 0.0, -(r["length_km"] or 0.0)))
    return rows


def _extract_table_rows(panel_tables, table_id: str):
    if not isinstance(panel_tables, list):
        return []
    for t in panel_tables:
        if isinstance(t, dict) and t.get("id") == table_id and isinstance(t.get("rows"), list):
            return t["rows"]
    return []


def _replace_table_rows(panel_tables, table_id: str, rows):
    if not isinstance(panel_tables, list):
        return
    for t in panel_tables:
        if isinstance(t, dict) and t.get("id") == table_id:
            t["rows"] = rows
            return


def _norm_name_for_match(v) -> str:
    if v is None:
        return ""
    s = str(v).strip().lower()
    if not s:
        return ""
    for token in ("省", "市", "区", "县", "特别行政区", "自治区", "自治县"):
        s = s.replace(token, "")
    return s.strip()


def _sync_emergency_district_locate_id(panel_tables, geojson):
    """
    对齐应急分区场景的 locate_id，避免表格与地图联动错位。
    优先按 locate_id 匹配，其次按 district_name 语义匹配，最后按顺序兜底。
    """
    rows = _extract_table_rows(panel_tables, "emergency_districts")
    feats = geojson.get("features") if isinstance(geojson, dict) else None
    if not isinstance(rows, list) or not rows:
        return
    if not isinstance(feats, list) or not feats:
        return

    by_lid = {}
    by_name = {}
    feature_items = []
    for idx, f in enumerate(feats):
        if not isinstance(f, dict):
            continue
        props = f.get("properties")
        if not isinstance(props, dict):
            props = {}
            f["properties"] = props
        lid = str(props.get("locate_id") or props.get("id") or "").strip()
        name = str(
            props.get("district_name")
            or props.get("name")
            or props.get("admin_name")
            or props.get("region_name")
            or ""
        ).strip()
        item = {"idx": idx, "props": props, "lid": lid, "name": name}
        feature_items.append(item)
        if lid:
            by_lid[lid] = item
        nkey = _norm_name_for_match(name)
        if nkey and nkey not in by_name:
            by_name[nkey] = item

    used_index = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row_lid = str(row.get("locate_id") or "").strip()
        row_name = str(
            row.get("district_name")
            or row.get("name")
            or row.get("admin_name")
            or row.get("region_name")
            or ""
        ).strip()

        target = None
        if row_lid and row_lid in by_lid:
            target = by_lid[row_lid]
        if target is None:
            nkey = _norm_name_for_match(row_name)
            if nkey and nkey in by_name:
                target = by_name[nkey]
        if target is None:
            for cand in feature_items:
                if cand["idx"] not in used_index:
                    target = cand
                    break

        if target is None:
            continue

        used_index.add(target["idx"])
        target_lid = str(target["props"].get("locate_id") or "").strip()
        if row_lid and target_lid and row_lid != target_lid:
            # 优先以表格 locate_id 为准，避免前端点击表格定位失败。
            target["props"]["locate_id"] = row_lid
            target_lid = row_lid
        elif row_lid and not target_lid:
            target["props"]["locate_id"] = row_lid
            target_lid = row_lid
        elif (not row_lid) and target_lid:
            row["locate_id"] = target_lid
            row_lid = target_lid

        if not row_lid and not target_lid:
            fallback_lid = f"emergency_district_{i+1}"
            row["locate_id"] = fallback_lid
            target["props"]["locate_id"] = fallback_lid

    _replace_table_rows(panel_tables, "emergency_districts", rows)


def _build_multiline_feature_from_geojson(river_name: str, locate_id: str, geojson: dict, length_km):
    if not isinstance(geojson, dict):
        return None
    feats = geojson.get("features")
    if not isinstance(feats, list) or not feats:
        return None

    lines = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        g = f.get("geometry")
        if not isinstance(g, dict):
            continue
        if g.get("type") == "LineString" and isinstance(g.get("coordinates"), list):
            lines.append(g.get("coordinates"))
        elif g.get("type") == "MultiLineString" and isinstance(g.get("coordinates"), list):
            lines.extend(g.get("coordinates"))

    if not lines:
        return None

    return {
        "type": "Feature",
        "geometry": {"type": "MultiLineString", "coordinates": lines},
        "properties": {
            "locate_id": str(locate_id),
            "river_name": river_name,
            "length_km": length_km,
            "distance_km": length_km,
        },
    }


def _extract_emergency_river_tables(data):
    if not isinstance(data, dict):
        return []

    # 优先使用新协议表结构（panel_tables/tables），避免被旧 direct/indirect 分支覆盖。
    raw_tables = None
    if isinstance(data.get("panel_tables"), list):
        raw_tables = data.get("panel_tables")
    elif isinstance(data.get("tables"), list):
        raw_tables = data.get("tables")

    if isinstance(raw_tables, list) and raw_tables:
        norm_tables = []
        for t in raw_tables:
            if not isinstance(t, dict):
                continue
            table_id = str(t.get("id") or t.get("table_key") or "table")
            title = str(t.get("title") or t.get("table_name") or table_id)

            src_cols = t.get("column_defs") if isinstance(t.get("column_defs"), list) else t.get("columns")
            cols = []
            if isinstance(src_cols, list):
                for c in src_cols:
                    if isinstance(c, dict):
                        key = str(c.get("key") or "").strip()
                        if key:
                            cols.append({"key": key, "label": c.get("label") or c.get("title") or key})
                    elif isinstance(c, str) and c.strip():
                        k = c.strip()
                        cols.append({"key": k, "label": k})

            rows = t.get("rows") if isinstance(t.get("rows"), list) else []
            safe_rows = []
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                rr = dict(r)
                if rr.get("locate_id") is None:
                    rr["locate_id"] = rr.get("feature_id") or rr.get("id") or f"{table_id}_{i+1}"
                safe_rows.append(rr)

            if not cols and safe_rows:
                cols = [{"key": k, "label": k} for k in safe_rows[0].keys()]

            if cols:
                norm_tables.append(_build_table(table_id, title, cols, safe_rows))

        if norm_tables:
            return norm_tables

    direct = data.get("direct_rivers") if isinstance(data.get("direct_rivers"), list) else []
    indirect = data.get("indirect_rivers") if isinstance(data.get("indirect_rivers"), list) else []
    if not direct and not indirect:
        return []

    def norm_rows(items, prefix):
        rows = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            rows.append(
                {
                    "river_name": it.get("river_name") or it.get("name"),
                    "length_km": it.get("length_km"),
                    "distance_km": it.get("distance_km") or it.get("distance"),
                    "locate_id": str(it.get("locate_id") or it.get("id") or f"{prefix}_{i+1}"),
                }
            )
        return rows

    cols = [
        {"key": "river_name", "label": "河名称"},
        {"key": "length_km", "label": "河长(km)"},
        {"key": "distance_km", "label": "距离(km)"},
        {"key": "locate_id", "label": "定位ID"},
    ]
    return [
        _build_table("direct_rivers", "直接影响河流", cols, norm_rows(direct, "direct")),
        _build_table("indirect_rivers", "间接影响河流", cols, norm_rows(indirect, "indirect")),
    ]


def _extract_emergency_river_geojson(data):
    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}

    features = []
    for group, color in [("direct_rivers", "#27AE60"), ("indirect_rivers", "#2F80ED")]:
        items = data.get(group) if isinstance(data.get(group), list) else []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            geom = it.get("geometry")
            if isinstance(geom, dict) and geom.get("type") and geom.get("coordinates"):
                features.append(
                    {
                        "type": "Feature",
                        "geometry": geom,
                        "properties": {
                            "group": group,
                            "style_color": color,
                            "river_name": it.get("river_name") or it.get("name"),
                            "length_km": it.get("length_km"),
                            "distance_km": it.get("distance_km") or it.get("distance"),
                            "locate_id": str(it.get("locate_id") or it.get("id") or f"{group}_{i+1}"),
                        },
                    }
                )
                continue

            # 兼容 from_x/from_y/to_x/to_y 的线段格式
            try:
                x1, y1 = float(it.get("from_x")), float(it.get("from_y"))
                x2, y2 = float(it.get("to_x")), float(it.get("to_y"))
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[x1, y1], [x2, y2]]},
                        "properties": {
                            "group": group,
                            "style_color": color,
                            "river_name": it.get("river_name") or it.get("name"),
                            "length_km": it.get("length_km"),
                            "distance_km": it.get("distance_km") or it.get("distance"),
                            "locate_id": str(it.get("locate_id") or it.get("id") or f"{group}_{i+1}"),
                        },
                    }
                )
            except Exception:
                continue

    return {"type": "FeatureCollection", "features": features}


def _extract_emergency_district_tables(data):
    if not isinstance(data, dict):
        return []

    # 优先读结构化分区/行政区字段
    candidates = (
        data.get("affected_districts")
        or data.get("affected_admin_units")
        or data.get("districts")
        or data.get("admin_units")
    )
    rows = []
    if isinstance(candidates, list):
        for i, it in enumerate(candidates):
            if not isinstance(it, dict):
                continue
            rows.append(
                {
                    "district_name": it.get("district_name") or it.get("name") or it.get("admin_name"),
                    "time_range": it.get("time_range") or it.get("times"),
                    "areal_rainfall_mm": it.get("areal_rainfall_mm") or it.get("accumulated_rainfall"),
                    "locate_id": str(it.get("locate_id") or it.get("id") or f"district_{i+1}"),
                }
            )

    # 兼容 basin_judgment：至少返回时段与等级信息，不让前端拿到空表
    if not rows and isinstance(data.get("slots"), list):
        for i, slot in enumerate(data.get("slots", [])):
            if not isinstance(slot, dict):
                continue
            judgment = slot.get("judgment") if isinstance(slot.get("judgment"), dict) else {}
            rows.append(
                {
                    "district_name": "海河流域",
                    "time_range": slot.get("times") or slot.get("times_compact"),
                    "areal_rainfall_mm": None,
                    "locate_id": str(slot.get("times") or f"slot_{i+1}"),
                    "level": judgment.get("level"),
                    "reached": judgment.get("reached"),
                }
            )

    cols = [
        {"key": "district_name", "label": "行政区"},
        {"key": "time_range", "label": "时段"},
        {"key": "areal_rainfall_mm", "label": "面累计降水量(mm)"},
        {"key": "locate_id", "label": "定位ID"},
    ]
    return [_build_table("emergency_districts", "应急影响行政区", cols, rows)] if rows else []


def _extract_emergency_partition_tables(data):
    if not isinstance(data, dict):
        return []

    # 优先读结构化分区字段，不与行政区混用。
    candidates = (
        data.get("affected_partitions")
        or data.get("partitions")
        or data.get("regions")
        or data.get("affected_regions")
    )
    rows = []
    if isinstance(candidates, list):
        for i, it in enumerate(candidates):
            if not isinstance(it, dict):
                continue
            rows.append(
                {
                    "partition_name": (
                        it.get("partition_name")
                        or it.get("region_name")
                        or it.get("zone_name")
                        or it.get("name")
                    ),
                    "time_range": it.get("time_range") or it.get("times"),
                    "areal_rainfall_mm": it.get("areal_rainfall_mm") or it.get("accumulated_rainfall"),
                    "locate_id": str(it.get("locate_id") or it.get("id") or f"partition_{i+1}"),
                }
            )

    cols = [
        {"key": "partition_name", "label": "分区"},
        {"key": "time_range", "label": "时段"},
        {"key": "areal_rainfall_mm", "label": "面累计降水量(mm)"},
        {"key": "locate_id", "label": "定位ID"},
    ]
    return [_build_table("emergency_partitions", "应急影响分区", cols, rows)] if rows else []


def _extract_emergency_district_geojson(data):
    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}

    # 优先使用标准 geojson
    if isinstance(data.get("geojson"), dict):
        return data.get("geojson")

    # 兼容 districts/admin_units 内嵌 geometry
    candidates = (
        data.get("affected_districts")
        or data.get("affected_admin_units")
        or data.get("districts")
        or data.get("admin_units")
        or []
    )
    features = []
    if isinstance(candidates, list):
        for i, it in enumerate(candidates):
            if not isinstance(it, dict):
                continue
            geom = it.get("geometry")
            if not (isinstance(geom, dict) and geom.get("type") and geom.get("coordinates")):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "district_name": it.get("district_name") or it.get("name") or it.get("admin_name"),
                        "time_range": it.get("time_range") or it.get("times"),
                        "areal_rainfall_mm": it.get("areal_rainfall_mm") or it.get("accumulated_rainfall"),
                        "locate_id": str(it.get("locate_id") or it.get("id") or f"district_{i+1}"),
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def _extract_emergency_partition_geojson(data):
    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}

    if isinstance(data.get("geojson"), dict):
        return data.get("geojson")

    candidates = (
        data.get("affected_partitions")
        or data.get("partitions")
        or data.get("regions")
        or data.get("affected_regions")
        or []
    )
    features = []
    if isinstance(candidates, list):
        for i, it in enumerate(candidates):
            if not isinstance(it, dict):
                continue
            geom = it.get("geometry")
            if not (isinstance(geom, dict) and geom.get("type") and geom.get("coordinates")):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "partition_name": (
                            it.get("partition_name")
                            or it.get("region_name")
                            or it.get("zone_name")
                            or it.get("name")
                        ),
                        "time_range": it.get("time_range") or it.get("times"),
                        "areal_rainfall_mm": it.get("areal_rainfall_mm") or it.get("accumulated_rainfall"),
                        "locate_id": str(it.get("locate_id") or it.get("id") or f"partition_{i+1}"),
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def _extract_geojson_and_tables(tool_name: str, observation, scene: str):
    data = _unwrap_tool_result(observation)
    data = _unwrap_scenario_http_payload(data)
    scenario_http_meta = data.get("meta") if isinstance(data, dict) and isinstance(data.get("meta"), dict) else None

    # emergency_http_server /scenario/*：标准 envelope（tables + map_geojson [+ meta.map_sql_id]）
    if (
        isinstance(data, dict)
        and isinstance(data.get("tables"), list)
        and ("map_geojson" in data or data.get("scenario"))
    ):
        http_scene = str(data.get("scenario") or "").strip()
        export_scene = _http_scenario_to_normalize_scene(http_scene)
        normalized = _normalize_scene_export_payload(export_scene, data)
        if normalized:
            ly = normalized["layers"]
            if isinstance(scenario_http_meta, dict) and isinstance(scenario_http_meta.get("map_sql_ids"), dict):
                if scenario_http_meta.get("map_sql_ids"):
                    ly = [
                        {"id": "emergency_districts", "color": "#8E44AD", "name": "应急影响行政区"},
                        {"id": "emergency_partitions", "color": "#9B59B6", "name": "应急影响分区"},
                    ]
            return (
                normalized["geojson"],
                normalized["panel_tables"],
                ly,
                scenario_http_meta,
            )

    geojson = {"type": "FeatureCollection", "features": []}
    tables = []
    layers = []

    # 河网工具：后端常返回河段列表，这里统一转 geojson
    if tool_name == "get_river_network_for_plot":
        geojson = _segments_to_geojson(data)
        if scene == "river_downstream":
            layers = [
                {"id": "current_river", "color": "#2F80ED", "name": "当前河流"},
                {"id": "downstream_rivers", "color": "#27AE60", "name": "下流河系"},
            ]
            rows = _aggregate_river_rows_from_geojson(geojson)
            tables.append(
                _build_table(
                    "downstream_rivers",
                    "下流河系列表",
                    [
                        {"key": "river_name", "label": "河名称"},
                        {"key": "length_km", "label": "河长(km)"},
                        {"key": "distance_km", "label": "距离(km)"},
                        {"key": "locate_id", "label": "定位ID"},
                    ],
                    rows,
                )
            )
        elif scene == "district_rivers":
            rows = []
            for f in geojson["features"]:
                p = f.get("properties", {})
                rows.append(
                    {
                        "river_name": p.get("river_name") or "未命名河段",
                        "length_km": p.get("length_km"),
                        "locate_id": p.get("locate_id"),
                    }
                )
            tables.append(_build_table("district_rivers", "河系列表", [
                {"key": "river_name", "label": "河名称"},
                {"key": "length_km", "label": "河长(km)"},
                {"key": "locate_id", "label": "定位ID"},
            ], rows))
    elif tool_name == "get_xialiu_rivername":
        # 兼容仅返回“逗号分隔河名”的场景，至少保证前端面板有可渲染表格
        names = []
        if isinstance(data, str):
            names = [s.strip() for s in data.split(",") if s.strip()]
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, dict):
                    n = str(item.get("name") or item.get("river") or "").strip()
                    if n:
                        names.append(n)
        elif isinstance(data, dict):
            raw_names = data.get("downstream_rivers") or data.get("rivers") or []
            if isinstance(raw_names, list):
                for item in raw_names:
                    if isinstance(item, str) and item.strip():
                        names.append(item.strip())
                    elif isinstance(item, dict):
                        n = str(item.get("name") or item.get("river") or "").strip()
                        if n:
                            names.append(n)

        if names:
            tables = [
                _build_table("downstream_rivers", "下流河系列表", [
                    {"key": "river_name", "label": "河名称"},
                    {"key": "length_km", "label": "河长(km)"},
                    {"key": "distance_km", "label": "距离(km)"},
                    {"key": "locate_id", "label": "定位ID"},
                ], [
                    {"river_name": n, "length_km": None, "distance_km": None, "locate_id": f"downstream_{i+1}"}
                    for i, n in enumerate(names)
                ]),
            ]

    # 行政区河流定位工具：返回行政区匹配结果 + 河流清单
    # 约定：panel.rows[*].locate_id 与 map.geojson.features[*].properties.locate_id 一致，方便前端联动。
    if tool_name == "locate_region_rivers":
        rows = []
        features = []

        def _split_river_names(name_text: str) -> list[str]:
            # 兼容“潮白新河,蓟运河”“京杭大运河、子牙河”这种复合名称
            if not isinstance(name_text, str):
                return []
            text = name_text.replace("，", ",").replace("、", ",").strip()
            if not text:
                return []
            return [s.strip() for s in text.split(",") if s.strip()]

        def _pick_first_numeric(item: dict, keys: list[str]):
            if not isinstance(item, dict):
                return None
            for k in keys:
                v = _to_float(item.get(k))
                if v is not None:
                    return v
            return None

        def _build_row_from_item(item: dict | str, i: int, default_locate_prefix: str):
            if isinstance(item, str):
                names = _split_river_names(item)
                return [
                    {
                        "river_name": n,
                        "length_km": None,
                        "distance_km": None,
                        "locate_id": f"{default_locate_prefix}_{i+1}_{j+1}",
                    }
                    for j, n in enumerate(names)
                ]

            if not isinstance(item, dict):
                return []

            raw_name = str(
                item.get("river_name")
                or item.get("name")
                or item.get("river")
                or item.get("river_names")
                or ""
            ).strip()
            names = _split_river_names(raw_name)
            if not names:
                return []

            length_km = _pick_first_numeric(item, ["length_km", "length", "river_length_km", "len_km"])
            distance_km = _pick_first_numeric(
                item,
                ["distance_km", "distance", "river_distance_km", "dist_km", "downstream_distance_km"],
            )
            if distance_km is None:
                # 业务口径：距离=河段长度
                distance_km = length_km
            # 某些工具只返回“河段数量”，先透传给长度列会误导，这里不强行映射到 length_km
            base_locate = str(item.get("locate_id") or item.get("id") or f"{default_locate_prefix}_{i+1}")

            built = []
            for j, n in enumerate(names):
                locate_id = base_locate if len(names) == 1 else f"{base_locate}_{j+1}"
                built.append(
                    {
                        "river_name": n,
                        "length_km": length_km,
                        "distance_km": distance_km,
                        "locate_id": locate_id,
                    }
                )
            return built

        if isinstance(data, dict):
            river_items = (
                data.get("rivers")
                or data.get("river_list")
                or data.get("matched_rivers")
                or data.get("river_names")
                or []
            )
            if isinstance(river_items, list):
                for i, item in enumerate(river_items):
                    rows.extend(_build_row_from_item(item, i, "district_river"))

            # 若工具直接给了 geojson，确保其 locate_id 与 rows 对齐
            raw_geojson = data.get("geojson")
            if isinstance(raw_geojson, dict) and isinstance(raw_geojson.get("features"), list):
                for idx, f in enumerate(raw_geojson.get("features", [])):
                    if not isinstance(f, dict):
                        continue
                    props = f.get("properties")
                    if not isinstance(props, dict):
                        props = {}
                        f["properties"] = props
                    props["locate_id"] = str(props.get("locate_id") or props.get("id") or f"district_river_{idx+1}")
                geojson = raw_geojson

            # 兼容工具返回“线段数组”格式（含 from_x/to_x 等）
            elif isinstance(data.get("segments"), list):
                geojson = _segments_to_geojson(data.get("segments"))

            # 如果有行数据但没有几何，前端仍可用 locate_id 做表内交互（地图层留空）
            if isinstance(geojson, dict) and isinstance(geojson.get("features"), list):
                # 用 geojson 属性回填 rows 的长度/距离，优先使用几何侧更可信数据
                by_locate = {}
                for f in geojson.get("features", []):
                    if not isinstance(f, dict):
                        continue
                    p = f.get("properties") if isinstance(f.get("properties"), dict) else {}
                    lid = str(p.get("locate_id") or "")
                    if lid:
                        by_locate[lid] = p
                if by_locate and rows:
                    for r in rows:
                        p = by_locate.get(str(r.get("locate_id") or ""))
                        if not p:
                            continue
                        if r.get("length_km") is None:
                            r["length_km"] = _to_float(p.get("length_km") or p.get("length"))
                        if r.get("distance_km") is None:
                            r["distance_km"] = _to_float(p.get("distance_km") or p.get("distance"))
                        if r.get("distance_km") is None:
                            r["distance_km"] = r.get("length_km")

        if rows:
            tables = [
                _build_table(
                    "district_rivers",
                    "河系列表",
                    [
                        {"key": "river_name", "label": "河名称"},
                        {"key": "length_km", "label": "河长(km)"},
                        {"key": "distance_km", "label": "距离(km)"},
                        {"key": "locate_id", "label": "定位ID"},
                    ],
                    rows,
                )
            ]
            layers = [{"id": "district_rivers", "color": "#2F80ED", "name": "分区河流"}]

    # 实时监测：站点温湿风压
    if scene == "realtime_station":
        station_rows = _extract_station_rows(data)
        if station_rows:
            geojson = _station_rows_to_geojson(station_rows)
            layers = [{"id": "national_stations", "color": "#E67E22", "name": "国家站点"}]
            table_rows = []
            for i, r in enumerate(station_rows):
                locate_id = r.get("locate_id") or r.get("station_id") or r.get("stcd") or r.get("id") or f"station_{i+1}"
                table_rows.append(
                    {
                        "station_name": r.get("station_name") or r.get("name"),
                        "temperature": r.get("temperature"),
                        "humidity": r.get("humidity"),
                        "wind": r.get("wind"),
                        "pressure": r.get("pressure"),
                        "locate_id": str(locate_id),
                    }
                )
            tables = [
                _build_table("station_observations", "站点观测数据", [
                    {"key": "station_name", "label": "站点名称"},
                    {"key": "temperature", "label": "温度"},
                    {"key": "humidity", "label": "湿度"},
                    {"key": "wind", "label": "风"},
                    {"key": "pressure", "label": "气压"},
                    {"key": "locate_id", "label": "定位ID"},
                ], table_rows)
            ]

    # 应急响应：河流/分区/判定明细
    if scene in {"emergency_rivers", "emergency_districts", "emergency_partitions"}:
        if scene == "emergency_rivers":
            emergency_tables = _extract_emergency_river_tables(data)
        elif scene == "emergency_partitions":
            emergency_tables = _extract_emergency_partition_tables(data)
        else:
            emergency_tables = _extract_emergency_district_tables(data)
        if not emergency_tables:
            emergency_tables = _extract_emergency_tables(data)
        if emergency_tables:
            tables = emergency_tables
        if scene == "emergency_rivers":
            layers = [
                {"id": "direct_rivers", "color": "#27AE60", "name": "直接影响河流"},
                {"id": "indirect_rivers", "color": "#2F80ED", "name": "间接影响河流"},
            ]
            geo_candidate = _extract_emergency_river_geojson(data)
            if isinstance(geo_candidate, dict) and geo_candidate.get("features"):
                geojson = geo_candidate
        elif scene == "emergency_partitions":
            layers = [{"id": "emergency_partitions", "color": "#8E44AD", "name": "应急影响分区"}]
            geo_candidate = _extract_emergency_partition_geojson(data)
            if isinstance(geo_candidate, dict) and geo_candidate.get("features"):
                geojson = geo_candidate
        else:
            layers = [{"id": "emergency_districts", "color": "#8E44AD", "name": "应急影响行政区"}]
            geo_candidate = _extract_emergency_district_geojson(data)
            if isinstance(geo_candidate, dict) and geo_candidate.get("features"):
                geojson = geo_candidate

    # 其它工具：优先透传已有标准结构
    if isinstance(data, dict):
        if isinstance(data.get("geojson"), dict) and (not isinstance(geojson, dict) or not geojson.get("features")):
            geojson = data["geojson"]
        if isinstance(data.get("panel_tables"), list) and not tables:
            tables = data["panel_tables"]
        if isinstance(data.get("layers"), list) and not layers:
            layers = data["layers"]

    return geojson, tables, layers, scenario_http_meta


_GIS_SQL_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _gis_sql_ident(name: str, default: str) -> str:
    n = (name or "").strip()
    if n and _GIS_SQL_IDENT_RE.match(n):
        return n
    return default


def _gis_vector_sql_enabled() -> bool:
    return os.getenv("GIS_VECTOR_SQL_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _gis_vector_sql_table() -> str:
    return _gis_sql_ident(os.getenv("GIS_VECTOR_SQL_TABLE", "gis_vector_sql_registry"), "gis_vector_sql_registry")


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _extract_province_for_vector_sql(data: dict | None, tool_args) -> str | None:
    if isinstance(data, dict):
        for k in ("province_name", "province", "prov_name"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(tool_args, dict):
        for k in ("province_name", "province"):
            v = tool_args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _build_admin_division_vector_sql(province_name: str) -> str:
    """
    与 WMS/GeoServer 侧视口参数一致的占位符：:srid :minx :miny :maxx :maxy。
    统一输出几何列，避免中间 GeoJSON 文本转换。
    """
    tbl = _gis_sql_ident(os.getenv("GIS_ADMIN_DIVISION_TABLE", "haihe_admin_division"), "haihe_admin_division")
    geom = _gis_sql_ident(os.getenv("GIS_ADMIN_DIVISION_GEOM_COLUMN", "geom"), "geom")
    pcol = _gis_sql_ident(os.getenv("GIS_ADMIN_DIVISION_PROVINCE_COLUMN", "province_name"), "province_name")
    prov = _sql_string_literal(province_name.strip())
    return (
        f"SELECT ST_Transform({geom}, :srid) AS geom\n"
        f"FROM {tbl}\n"
        f"WHERE {pcol} = {prov}\n"
        f"AND ST_Intersects(\n"
        f"  ST_Transform({geom}, :srid),\n"
        f"  ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, :srid)\n"
        f")"
    )


def _resolve_postgis_vector_sql_text(scene: str, tool_args, observation) -> str | None:
    """
    优先使用工具返回的完整 SQL（postgis_vector_sql / vector_sql_text）；
    否则在应急行政区场景下按省名拼 haihe_admin_division 类查询。
    """
    data = _unwrap_tool_result(observation)
    if isinstance(data, dict):
        raw = data.get("postgis_vector_sql") or data.get("vector_sql_text")
        if isinstance(raw, str) and raw.strip():
            q = raw.strip()
            # 统一按 WMS 直连 SQL 处理：不再强依赖 ST_AsGeoJSON，几何列/WKB/GeoJSON 均可被服务端解析。
            if "ST_Intersects" in q and "ST_MakeEnvelope" in q:
                return q
            print("[GIS vector SQL] 工具返回 SQL 缺少 ST_Intersects/ST_MakeEnvelope，已忽略。")

    if scene == "emergency_districts":
        prov = _extract_province_for_vector_sql(data if isinstance(data, dict) else None, tool_args)
        if prov:
            return _build_admin_division_vector_sql(prov)

    return None


def _wms_descriptor(vector_sql_id: int) -> dict:
    base = os.getenv("GIS_WMS_BASE_URL", "").strip()
    param = (os.getenv("GIS_WMS_SQL_ID_PARAM", "sql_id").strip() or "sql_id")
    layer_name = os.getenv("GIS_WMS_LAYER_NAME", "").strip()
    d: dict = {
        "base_url": base,
        "sql_id_param": param,
        "vector_sql_id": vector_sql_id,
    }
    if layer_name:
        d["layer"] = layer_name
    return d


def _wms_descriptor_multi(vector_sql_ids: dict) -> dict:
    """双图层（行政区 + 分区）等多 sql id，供父页面按 key 分别拼 WMS。"""
    base = os.getenv("GIS_WMS_BASE_URL", "").strip()
    param = (os.getenv("GIS_WMS_SQL_ID_PARAM", "sql_id").strip() or "sql_id")
    layer_name = os.getenv("GIS_WMS_LAYER_NAME", "").strip()
    d: dict = {
        "base_url": base,
        "sql_id_param": param,
        "vector_sql_ids": {str(k): int(v) for k, v in vector_sql_ids.items()},
    }
    if layer_name:
        d["layer"] = layer_name
    return d


async def _register_gis_vector_sql_row(sql_text: str, scene: str, linkage_id: str) -> int | None:
    data_layer = get_data_layer()
    if not isinstance(data_layer, SQLAlchemyDataLayer):
        return None
    table = _gis_vector_sql_table()
    q = f'INSERT INTO "{table}" (sql_text, scene, linkage_id) VALUES (:sql_text, :scene, :linkage_id) RETURNING id'
    try:
        rows = await data_layer.execute_sql(
            q,
            {"sql_text": sql_text, "scene": scene, "linkage_id": linkage_id},
        )
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            rid = rows[0].get("id")
            if rid is not None:
                return int(rid)
    except Exception as e:
        print(f"[GIS vector SQL 注册失败] table={table} err={e}")
    return None


async def _emit_gis_packet(packet: dict):
    """
    统一 GIS 联动包发送出口（同构协议）：
    - 打印 [GIS_JSON] 供控制台排障
    - 发送 window_message 给前端
    - （可选）发送 socket 广播事件（gis_linkage）
    """
    payload_str = json.dumps(packet, ensure_ascii=False)
    print(f"[GIS_JSON]{payload_str}")
    await cl.send_window_message(payload_str)
    # 默认关闭 socket 双事件广播，避免前端收到重复/三次日志。
    # 如需给非 window_message 客户端兼容，可设置 GIS_EMIT_SOCKET_GIS_LINKAGE=1。
    if os.getenv("GIS_EMIT_SOCKET_GIS_LINKAGE", "0").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from chainlit.server import sio
            await sio.emit("gis_linkage", {"message": payload_str})
        except Exception as e:
            print(f"[GIS广播] 发送失败：{e}")


def _extract_thematic_png_rows_from_job_payload(job_payload: dict, is_obs_mode: bool, req_hours: list[int]) -> list[dict]:
    rows: list[dict] = []
    if not isinstance(job_payload, dict):
        return rows
    st = str(job_payload.get("status") or "").strip().lower()
    result = job_payload.get("result") if isinstance(job_payload.get("result"), dict) else {}
    products = result.get("products") if isinstance(result.get("products"), list) else []
    for p in products:
        if not isinstance(p, dict):
            continue
        if p.get("ok") is False:
            continue
        h = p.get("accum_hours") if is_obs_mode else p.get("lead_hours")
        url_hint = str(p.get("url_hint") or "").strip()
        if not h or not url_hint:
            continue
        rows.append(
            {
                "mode": "实况影响" if is_obs_mode else "预报影响",
                "hours": int(h),
                "title": f"{'实况' if is_obs_mode else '预报'}累计降水（{int(h)}h）",
                "png_url": url_hint,
            }
        )
    if rows:
        return rows
    # 兜底仅在成功态下启用：若 result.products 缺失，按任务元信息拼出标准 png 接口 URL
    if st not in {"done", "partial_failed"}:
        return rows
    if is_obs_mode:
        tc = str(job_payload.get("times_compact") or "").strip()
        if tc:
            for h in req_hours:
                rows.append(
                    {
                        "mode": "实况影响",
                        "hours": int(h),
                        "title": f"实况累计降水（{int(h)}h）",
                        "png_url": f"/emergency/observation/products/png?times_compact={tc}&accum_hours={int(h)}",
                    }
                )
    else:
        sc = str(job_payload.get("start_time_compact") or "").strip()
        if sc:
            for h in req_hours:
                rows.append(
                    {
                        "mode": "预报影响",
                        "hours": int(h),
                        "title": f"预报累计降水（{int(h)}h）",
                        "png_url": f"/emergency/forecast/products/png?start_time_compact={sc}&lead_hours={int(h)}",
                    }
                )
    return rows


async def _watch_thematic_job_and_emit_result(
    *,
    base_url: str,
    timeout_sec: int,
    is_obs_mode: bool,
    job_id: str,
    req_hours: list[int],
    query_text: str,
    emit_gis_packet: bool = True,
):
    if not job_id:
        return
    status_url = (
        f"{base_url}/emergency/observation/products/jobs/{job_id}"
        if is_obs_mode
        else f"{base_url}/emergency/forecast/products/jobs/{job_id}"
    )
    max_polls = 30
    interval_sec = 2
    for _ in range(max_polls):
        await asyncio.sleep(interval_sec)
        try:
            job_payload = await _http_get_json(status_url, timeout_sec=timeout_sec)
        except Exception:
            continue
        st = str((job_payload or {}).get("status") or "").strip().lower()
        if st in {"done", "failed", "partial_failed"}:
            png_rows = _extract_thematic_png_rows_from_job_payload(job_payload if isinstance(job_payload, dict) else {}, is_obs_mode, req_hours)
            fail_reasons: list[dict] = []
            if isinstance(job_payload, dict):
                for it in (job_payload.get("items") or []):
                    if not isinstance(it, dict):
                        continue
                    if it.get("ok") is False:
                        h = it.get("accum_hours") if is_obs_mode else it.get("lead_hours")
                        fail_reasons.append(
                            {
                                "mode": "实况影响" if is_obs_mode else "预报影响",
                                "hours": int(h) if h is not None else None,
                                "reason": str(it.get("message") or "该时效专题图生成失败"),
                            }
                        )
            packet = {
                "type": "gis_linkage",
                "schema_version": "v2",
                "linkage_id": f"gis_thematic_result_{uuid4().hex[:10]}",
                "scene": "rainfall_thematic_result_observation" if is_obs_mode else "rainfall_thematic_result_forecast",
                "scene_key": "scene.rainfall_thematic_result_observation" if is_obs_mode else "scene.rainfall_thematic_result_forecast",
                "query": query_text,
                "tool": {"name": "thematic_job_status_watch", "args": {"job_id": job_id}},
                "meta": {
                    "source": "chain_gzt",
                    "emitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "route_key": "rainfall_thematic_result:job_watch",
                    "data_origin": "thematic_job_result",
                },
                "map": {"render_mode": "none", "geojson": None, "layers": []},
                "panel": {
                    "tables": (
                        [
                        {
                            "table_key": "thematic_png_results",
                            "table_name": "专题图结果",
                            "columns": [
                                {"key": "mode", "title": "口径"},
                                {"key": "hours", "title": "时效(小时)"},
                                {"key": "title", "title": "专题图"},
                                {"key": "png_url", "title": "查看链接"},
                            ],
                            "rows": png_rows,
                        }
                        ]
                        if png_rows
                        else []
                    )
                    + (
                        [
                        {
                            "table_key": "thematic_generation_failures",
                            "table_name": "专题图失败原因",
                            "columns": [
                                {"key": "mode", "title": "口径"},
                                {"key": "hours", "title": "时效(小时)"},
                                {"key": "reason", "title": "原因"},
                            ],
                            "rows": fail_reasons,
                        }
                        ]
                        if fail_reasons
                        else []
                    )
                },
            }
            if emit_gis_packet:
                await _emit_gis_packet(packet)
            if png_rows:
                base_http = (base_url or "").rstrip("/")
                ordered_rows = sorted(png_rows, key=lambda x: int(x.get("hours") or 0))
                images = []
                for row in ordered_rows:
                    raw_url = str(row.get("png_url") or "").strip()
                    if raw_url.startswith("http://") or raw_url.startswith("https://"):
                        full_url = raw_url
                    elif raw_url.startswith("/"):
                        full_url = f"{base_http}{raw_url}" if base_http else raw_url
                    else:
                        full_url = f"{base_http}/{raw_url}" if base_http else raw_url
                    title = str(row.get("title") or "专题图").strip() or "专题图"
                    images.append(cl.Image(name=title, url=full_url))
                await cl.Message(content="专题图结果如下：", elements=images, author="GIS问答").send()
            if st == "failed":
                await cl.Message(content="专题图生成失败，请稍后重试。", author="GIS问答").send()
            return


def _start_thematic_watch_task(
    *,
    base_url: str,
    timeout_sec: int,
    is_obs_mode: bool,
    job_id: str,
    req_hours: list[int],
    query_text: str,
    emit_gis_packet: bool = True,
) -> bool:
    """同一 job_id 仅保留一个 watcher，避免重复推送“专题图已生成”消息。"""
    key = str(job_id or "").strip()
    if not key:
        return False
    task_map = cl.user_session.get("thematic_watch_tasks")
    if not isinstance(task_map, dict):
        task_map = {}
    existing = task_map.get(key)
    if isinstance(existing, asyncio.Task) and (not existing.done()):
        return False
    task = asyncio.create_task(
        _watch_thematic_job_and_emit_result(
            base_url=base_url,
            timeout_sec=timeout_sec,
            is_obs_mode=is_obs_mode,
            job_id=key,
            req_hours=req_hours,
            query_text=query_text,
            emit_gis_packet=emit_gis_packet,
        )
    )
    task_map[key] = task
    cl.user_session.set("thematic_watch_tasks", task_map)

    def _cleanup(_done_task):
        tm = cl.user_session.get("thematic_watch_tasks")
        if not isinstance(tm, dict):
            return
        cur = tm.get(key)
        if cur is _done_task:
            tm.pop(key, None)
            cl.user_session.set("thematic_watch_tasks", tm)

    task.add_done_callback(_cleanup)
    return True


async def _send_gis_linkage(tool_name: str, tool_args, observation, user_text: str, tools=None):
    scene = _guess_gis_scene(user_text)
    geojson, panel_tables, layers, scenario_http_meta = _extract_geojson_and_tables(tool_name, observation, scene)
    http_wms_sql = False
    if isinstance(scenario_http_meta, dict):
        map_render = str(scenario_http_meta.get("map_render") or "").strip().lower()
        has_sql_id = scenario_http_meta.get("map_sql_id") is not None
        has_sql_ids = isinstance(scenario_http_meta.get("map_sql_ids"), dict) and bool(scenario_http_meta.get("map_sql_ids"))
        # 兼容后端仅回 map_sql_id/map_sql_ids（未显式给 map_render）的场景，仍按 wms_sql 处理
        http_wms_sql = (map_render == "wms_sql") or has_sql_id or has_sql_ids
    data_origin = "mcp_tool"
    source_file = None
    source_path = None
    slot_times = None
    # 实时监测/应急场景：内网工具结果为空或不完整时，可回退到本地 scene_exports JSON。
    # 其中 emergency_rivers 默认不回退，避免把实时查询误替换为历史样例数据；
    # 仅当请求显式 force_local=1 时才允许回退。
    allow_local_fallback = scene in {"realtime_station", "emergency_rivers", "emergency_districts", "emergency_partitions"}
    if scene == "emergency_rivers":
        force_local_flag = False
        if isinstance(tool_args, dict):
            force_local_flag = str(tool_args.get("force_local", "")).strip().lower() in {"1", "true", "yes", "on"}
        allow_local_fallback = force_local_flag
    if allow_local_fallback:
        need_local_fallback = (not http_wms_sql) and (
            not isinstance(geojson, dict)
            or not geojson.get("features")
            or not isinstance(panel_tables, list)
            or not panel_tables
        )
        if need_local_fallback:
            local_payload = _load_local_scene_export(scene, tool_args, user_text)
            if isinstance(local_payload, dict):
                local_meta = local_payload.get("_local_meta") if isinstance(local_payload.get("_local_meta"), dict) else {}
                data_origin = str(local_meta.get("data_origin") or "local_scene_export")
                source_file = local_meta.get("source_file")
                source_path = local_meta.get("source_path")
                slot_times = local_meta.get("slot_times")
                geojson, panel_tables, layers, scenario_http_meta = _extract_geojson_and_tables(
                    "local_scene_export", local_payload, scene
                )
                if isinstance(scenario_http_meta, dict):
                    map_render = str(scenario_http_meta.get("map_render") or "").strip().lower()
                    has_sql_id = scenario_http_meta.get("map_sql_id") is not None
                    has_sql_ids = isinstance(scenario_http_meta.get("map_sql_ids"), dict) and bool(scenario_http_meta.get("map_sql_ids"))
                    http_wms_sql = (map_render == "wms_sql") or has_sql_id or has_sql_ids
                else:
                    http_wms_sql = False
    # 下流河系场景：若本轮只拿到文本型下游河名（无空间要素），自动补调河网工具拉空间线段
    if (
        scene == "river_downstream"
        and tool_name == "get_xialiu_rivername"
        and isinstance(geojson, dict)
        and not geojson.get("features")
        and not http_wms_sql
    ):
        candidate_tools = tools if isinstance(tools, list) else cl.user_session.get("tools")
        if isinstance(candidate_tools, list):
            river_tool = next((t for t in candidate_tools if getattr(t, "name", "") == "get_river_network_for_plot"), None)
            if river_tool is not None:
                river_name = ""
                if isinstance(tool_args, dict):
                    river_name = str(tool_args.get("river") or tool_args.get("start_river") or "").strip()
                if not river_name:
                    river_name = _extract_river_name(user_text)
                if river_name:
                    try:
                        river_obs = await river_tool.ainvoke({"start_river": river_name})
                        river_geojson, _, river_layers, _ = _extract_geojson_and_tables(
                            "get_river_network_for_plot", river_obs, scene
                        )
                        if isinstance(river_geojson, dict) and river_geojson.get("features"):
                            geojson = river_geojson
                        if river_layers:
                            layers = river_layers
                    except Exception as e:
                        print(f"[GIS补拉河网] 失败：{e}")

    # 下流河系场景：若已拿到河网几何，则用几何聚合结果覆盖面板表，
    # 避免出现“地图有长度/距离，表格为空值”的不一致。
    if scene == "river_downstream" and isinstance(geojson, dict) and geojson.get("features") and not http_wms_sql:
        agg_rows = _aggregate_river_rows_from_geojson(geojson)
        if agg_rows:
            panel_tables = [
                _build_table(
                    "downstream_rivers",
                    "下流河系列表",
                    [
                        {"key": "river_name", "label": "河名称"},
                        {"key": "length_km", "label": "河长(km)"},
                        {"key": "distance_km", "label": "距离(km)"},
                        {"key": "locate_id", "label": "定位ID"},
                    ],
                    agg_rows,
                )
            ]

    # 分区河流场景：若 locate_region_rivers 仅返回河名，自动逐河补调河网以回填长度/距离，
    # 并构建与表格 locate_id 对齐的地图要素，方便前端点击联动。
    if scene == "district_rivers" and tool_name == "locate_region_rivers":
        rows = _extract_table_rows(panel_tables, "district_rivers")
        candidate_tools = tools if isinstance(tools, list) else cl.user_session.get("tools")
        river_tool = None
        if isinstance(candidate_tools, list):
            river_tool = next((t for t in candidate_tools if getattr(t, "name", "") == "get_river_network_for_plot"), None)

        if rows and river_tool is not None:
            merged_features = []
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                # 已有长度则不再重复补查
                if _to_float(r.get("length_km")) is not None:
                    continue
                river_name = str(r.get("river_name") or "").strip()
                if not river_name:
                    continue
                try:
                    river_obs = await river_tool.ainvoke({"start_river": river_name})
                    river_geojson, _, _, _ = _extract_geojson_and_tables("get_river_network_for_plot", river_obs, "district_rivers")
                    agg = _aggregate_river_rows_from_geojson(river_geojson)
                    length_km = _to_float(agg[0].get("length_km")) if agg else None
                    r["length_km"] = length_km
                    r["distance_km"] = length_km  # 业务口径：距离=河长

                    locate_id = str(r.get("locate_id") or f"district_river_{i+1}")
                    f = _build_multiline_feature_from_geojson(river_name, locate_id, river_geojson, length_km)
                    if f:
                        merged_features.append(f)
                except Exception as e:
                    print(f"[GIS补拉分区河流] {river_name} 失败：{e}")

            _replace_table_rows(panel_tables, "district_rivers", rows)
            if merged_features:
                geojson = {"type": "FeatureCollection", "features": merged_features}
                layers = [{"id": "district_rivers", "color": "#2F80ED", "name": "分区河流"}]

    if scene == "emergency_districts":
        _sync_emergency_district_locate_id(panel_tables, geojson)

    linkage_id = f"gis_{scene}_{uuid4().hex[:10]}"

    map_payload: dict = {
        "geojson": geojson,
        "layers": layers,
        "render_mode": "geojson",
    }
    # 应急 HTTP 已在服务端登记 hh_gis_wms_sql，直接透传 id（与 GIS_VECTOR_SQL_ENABLED 无关）
    if http_wms_sql and isinstance(scenario_http_meta, dict):
        mids_raw = scenario_http_meta.get("map_sql_ids")
        mid = scenario_http_meta.get("map_sql_id")
        if isinstance(mids_raw, dict) and mids_raw:
            map_payload["render_mode"] = "wms_sql_id"
            map_payload["vector_sql_ids"] = {str(k): int(v) for k, v in mids_raw.items()}
            map_payload["geojson"] = None
            map_payload["wms"] = _wms_descriptor_multi(mids_raw)
            map_payload["scenario_http_meta"] = {
                "map_render": "wms_sql",
                "wms_sql_params": scenario_http_meta.get("wms_sql_params"),
            }
        elif mid is not None:
            map_payload["render_mode"] = "wms_sql_id"
            map_payload["vector_sql_id"] = int(mid)
            map_payload["geojson"] = None
            map_payload["wms"] = _wms_descriptor(int(mid))
            map_payload["scenario_http_meta"] = {
                "map_render": "wms_sql",
                "wms_sql_params": scenario_http_meta.get("wms_sql_params"),
            }
    elif _gis_vector_sql_enabled():
        sql_text = _resolve_postgis_vector_sql_text(scene, tool_args, observation)
        if sql_text:
            registered_id = await _register_gis_vector_sql_row(sql_text, scene, linkage_id)
            if registered_id is not None:
                map_payload["render_mode"] = "wms_sql_id"
                map_payload["vector_sql_id"] = registered_id
                map_payload["geojson"] = None
                map_payload["wms"] = _wms_descriptor(registered_id)

    packet = {
        "type": "gis_linkage",
        "schema_version": "v2",
        # 供前端快速检索/排障的稳定标识
        "linkage_id": linkage_id,
        "scene": scene,
        "scene_key": f"scene.{scene}",
        "query": user_text,
        "tool": {"name": tool_name, "args": tool_args},
        "meta": {
            "source": "chain_gzt",
            "emitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "route_key": f"{scene}:{tool_name}",
            "data_origin": data_origin,
            "source_file": source_file,
            "source_path": source_path,
            "slot_times": _format_compact_time_text(slot_times),
        },
        "map": map_payload,
        "panel": {
            "tables": panel_tables,
        },
    }

    await _emit_gis_packet(packet)


def _build_river_network_brief(raw_result, river_name: str) -> str:
    """基于河网结果生成更细的领导简报（不依赖二次LLM）"""
    segments = _unwrap_tool_result(raw_result)
    if not isinstance(segments, list) or not segments:
        return f"`{river_name}` 暂无可用河网数据，建议稍后重试。"

    valid = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            order = int(seg.get("strahler_order", 1))
            name = (seg.get("rivername") or "").strip()

            def _push_line(line):
                if not isinstance(line, list) or len(line) < 2:
                    return
                pts = [(float(p[0]), float(p[1])) for p in line if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(pts) < 2:
                    return
                x1, y1 = pts[0]
                x2, y2 = pts[-1]
                valid.append((x1, y1, x2, y2, order, name))

            geom = seg.get("geometry")
            if isinstance(geom, dict):
                gtype = geom.get("type")
                coords = geom.get("coordinates")
                if gtype == "LineString" and isinstance(coords, list):
                    _push_line(coords)
                    continue
                if gtype == "MultiLineString" and isinstance(coords, list):
                    for ln in coords:
                        _push_line(ln)
                    continue

            if isinstance(seg.get("paths"), list):
                for ln in seg.get("paths", []):
                    _push_line(ln)
                continue
            if isinstance(seg.get("path"), list):
                _push_line(seg.get("path"))
                continue

            x1, y1 = float(seg["from_x"]), float(seg["from_y"])
            x2, y2 = float(seg["to_x"]), float(seg["to_y"])
            valid.append((x1, y1, x2, y2, order, name))
        except Exception:
            continue

    if not valid:
        return f"`{river_name}` 河网数据格式异常，建议稍后重试。"

    from collections import Counter, defaultdict
    import math
    in_degree = defaultdict(int)
    out_degree = defaultdict(int)
    order_counter = Counter()
    river_counter = Counter()
    river_len_km = defaultdict(float)
    total_len_km = 0.0

    for x1, y1, x2, y2, order, name in valid:
        out_degree[(x1, y1)] += 1
        in_degree[(x2, y2)] += 1
        order_counter[order] += 1

        # 粗略长度估算：经纬度差按局地尺度换算为公里（用于相对比较）
        mean_lat = (y1 + y2) / 2.0
        dx_km = (x2 - x1) * 111.0 * math.cos(math.radians(mean_lat))
        dy_km = (y2 - y1) * 111.0
        seg_len = math.sqrt(dx_km * dx_km + dy_km * dy_km)
        total_len_km += seg_len

        if name and name not in {"未知", "None"}:
            river_counter[name] += 1
            river_len_km[name] += seg_len

    nodes = set(in_degree.keys()) | set(out_degree.keys())
    sources = sum(1 for n in nodes if in_degree[n] == 0)
    sinks = sum(1 for n in nodes if out_degree[n] == 0)
    confluences = sum(1 for n in nodes if in_degree[n] > 1)
    avg_len_km = total_len_km / len(valid) if valid else 0.0

    # 等级结构：全部等级逐条列出（无「等」省略）
    all_orders = "；".join(
        [f"{k}级共{v}段" for k, v in sorted(order_counter.items(), key=lambda x: x[0])]
    ) or "待核实"

    unnamed_seg = sum(1 for *_, name in valid if not name or name in {"未知", "None"})
    named_count = len(river_counter)

    # 摘要行仍给领导看；长度/段数前三仅作「摘要」，完整表见下文
    top_len_rivers = "、".join(
        [f"{k}({v:.1f}km)" for k, v in sorted(river_len_km.items(), key=lambda x: x[1], reverse=True)[:3]]
    ) or "（无具名河段）"
    complexity_ratio = (confluences / len(nodes) * 100.0) if nodes else 0.0

    # 完整具名河段清单（按估算河长降序，专业人士可核对无遗漏）
    detail_lines = [
        "| 河流名称 | 河段数 | 估算河长(km) |",
        "| :--- | :--- | :--- |",
    ]
    for rname, rlen in sorted(river_len_km.items(), key=lambda x: (-x[1], x[0])):
        detail_lines.append(f"| {rname} | {river_counter[rname]} | {rlen:.2f} |")
    if unnamed_seg:
        detail_lines.append(f"| （河段无有效名称） | {unnamed_seg} | — |")
    if len(detail_lines) == 2:
        detail_lines.append("| （本次河段均无有效名称，无法逐河列举） | — | — |")

    full_table = "\n".join(detail_lines)
    detail_note = (
        "**完整河段统计表（上表已全部列出，无「等」省略）**\n\n"
        f"* 具名河流 {named_count} 条；无名称河段 {unnamed_seg} 段。\n\n"
    )

    return (
        f"`{river_name}` 河网影响简报：共识别 {len(valid)} 条河段、{len(nodes)} 个节点，"
        f"估算河网总长约 {total_len_km:.1f} km。\n\n"
        "| 关注点 | 现状 | 风险等级 |\n"
        "| :--- | :--- | :--- |\n"
        f"| 河段规模 | {len(valid)} 条；平均每段 {avg_len_km:.2f} km；具名河流 {named_count} 条 | 中 |\n"
        f"| 河网长度 | 总长约 {total_len_km:.1f} km；河长前三（摘要）：{top_len_rivers} | 中 |\n"
        f"| 汇流复杂度 | 汇流点 {confluences} 个，复杂度 {complexity_ratio:.1f}%（汇流点/节点） | 中-高 |\n"
        f"| 源汇分布 | 源头 {sources} 个，末端 {sinks} 个，需重点盯末端卡口 | 中 |\n"
        f"| 水系等级结构（全量） | {all_orders} | 中 |\n\n"
        f"{detail_note}"
        f"{full_table}\n\n"
        "建议行动：\n"
        "1) 先盯汇流点与末端卡口，布设高频巡查；\n"
        "2) 按上表具名河段与等级结构加密监测；\n"
        "3) 每30分钟滚动复核雨情、水情和积涝反馈。"
    )

# ===============================
# 1. 会话开始
# ===============================
@cl.on_chat_start
async def on_chat_start():
    await _init_runtime_session(messages_seed=[])
    # await cl.Message(
    #     content="您好，我是海河流域智能问答，请告诉我您的需求。"
    # ).send()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    resumed_messages = _build_messages_from_thread(thread)
    await _init_runtime_session(messages_seed=resumed_messages)


def _is_gis_timeline_alert_payload(d) -> bool:
    """识别大屏时间轴/预警推送 JSON（与前端 sendData 约定一致即可触发会话内推送）。"""
    if not isinstance(d, dict):
        return False
    if d.get("event_code") and (d.get("timeline_phase") is not None or d.get("ui_dialog")):
        return True
    ext = d.get("ext")
    if isinstance(ext, dict) and ext.get("response_ui") and d.get("title"):
        return True
    return False


def _gis_alert_subject_line(d: dict) -> str:
    """首段里的「主体」描述：优先用大屏 title，避免再拼营销长句。"""
    t = (d.get("title") or "").strip()
    if t:
        return t
    ui = d.get("ui_dialog") if isinstance(d.get("ui_dialog"), dict) else {}
    return (ui.get("title") or "").strip() or "当前预警事件"


def _gis_alert_level_phrase(d: dict) -> str:
    lv = (d.get("level_display") or d.get("event_level") or "").strip()
    if not lv:
        return "相应级别"
    if "应急" in lv:
        return lv
    return f"{lv}应急响应"


def _infer_emergency_alert_scene(d: dict) -> str:
    """
    统一识别 6 类提示场景：
    - forecast_trigger_pending: 基于预报触发（当前无响应，待确认）
    - observation_trigger_pending: 基于实况触发（当前无响应，待确认）
    - response_published: 应急响应已发布（不再提示“确认发布”）
    - forecast_response_ongoing: 预报应急响应中
    - observation_response_ongoing: 实况应急响应中
    - response_ended: 应急响应结束
    """
    phase = str(d.get("timeline_phase") or "").strip().lower()
    status = str(d.get("status") or "").strip().lower()
    status_disp = str(d.get("status_display") or "").strip()
    ext = d.get("ext") if isinstance(d.get("ext"), dict) else {}
    source = str(ext.get("source") or d.get("source") or "").strip().lower()
    wf = ext.get("workflow") if isinstance(ext.get("workflow"), dict) else {}
    ack = wf.get("publish_ack") if isinstance(wf.get("publish_ack"), dict) else {}
    publish_status = str(
        d.get("publish_status")
        or ack.get("status")
        or d.get("publish_status_display")
        or ""
    ).strip().lower()
    published = (
        publish_status in {"yes", "已发布", "published", "true", "1"}
        or ack.get("published") is True
    )

    if status in {"archived", "closed", "ended", "done"} or "归档" in status_disp or phase == "past":
        return "response_ended"

    if published:
        return "response_published"

    is_observation = ("observation" in source) or ("实况" in str(d.get("title") or ""))
    is_forecast = ("forecast" in source) or ("预报" in str(d.get("title") or ""))

    if phase in {"now", "ongoing"}:
        if is_observation:
            return "observation_response_ongoing"
        return "forecast_response_ongoing"

    # future_hours 或未知时默认按“待确认触发”处理
    if is_observation:
        return "observation_trigger_pending"
    if is_forecast:
        return "forecast_trigger_pending"
    return "forecast_trigger_pending"


def _gis_alert_followup_lines(d: dict) -> list[str]:
    scene = _infer_emergency_alert_scene(d)
    if scene == "forecast_trigger_pending":
        return [
            "1、帮你获取未来24、48、72小时的面雨量，并进行展示；",
            "2、帮你查看可能影响的河流水系有哪些；",
            "3、告诉我确认发布Ⅳ级应急响应，或者发布其他级别应急响应；",
            "4、不启动应急响应。",
        ]
    if scene == "observation_trigger_pending":
        return [
            "1、帮你获取过去12、24小时的面雨量，并进行展示；",
            "2、帮你查看可能影响的河流水系有哪些；",
            "3、告诉我确认发布Ⅳ级应急响应，或者发布其他级别应急响应。",
        ]
    if scene == "forecast_response_ongoing":
        return [
            "1、帮你获取未来24、48、72小时的面雨量，并进行展示；",
            "2、帮你制作预报相关的报告；",
            "3、是否需要修改等级。",
        ]
    if scene == "observation_response_ongoing":
        return [
            "1、帮你获取未来24、48、72小时的面雨量，并进行展示；",
            "2、帮你制作应急响应相关的报告；",
            "3、停止当前应急响应。",
        ]
    if scene == "response_published":
        return [
            "1、帮你获取未来24、48、72小时的面雨量，并进行展示；",
            "2、帮你查看当前受影响的行政区和河流水系；",
            "3、帮你生成已发布响应的跟踪简报。",
        ]
    return [
        "1、查询历史应急响应记录；",
        "2、查看本次应急响应结束前后的雨情变化；",
        "3、查看关联河系与影响区域复盘信息。",
    ]


def _format_gis_alert_assistant_template(d: dict) -> str:
    """
    按大屏模板输出：①叙述段（字段填槽，不用 ui_dialog.body 整段当正文）；②固定引导；③编号可操作建议。
    """
    raw_when = (d.get("effective_start_time") or d.get("start_time") or "").strip()
    t_when = raw_when or "（时间待大屏补充）"
    # 业务侧要求按整点表达：如 2026-05-07 19:08:00 -> 2026-05-07 19:00:00
    if raw_when:
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d %H",
        ):
            try:
                dt = datetime.strptime(raw_when, fmt)
                break
            except Exception:
                continue
        if dt is not None:
            t_when = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    level_phr = _gis_alert_level_phrase(d)
    scene = _infer_emergency_alert_scene(d)
    if scene == "forecast_trigger_pending":
        para1 = f"根据预报数据，海河流域{t_when}满足{level_phr}，需要启动应急响应。"
    elif scene == "observation_trigger_pending":
        para1 = f"根据前一时刻，海河流域{t_when}满足{level_phr}，需要启动应急响应。"
    elif scene == "forecast_response_ongoing":
        para1 = f"海河流域预计{t_when}进入{level_phr}。"
    elif scene == "observation_response_ongoing":
        para1 = "当前海河流域正处于应急响应中，如有需要可继续执行联动操作。"
    elif scene == "response_published":
        para1 = f"该应急响应已正式发布（{level_phr}），可继续跟踪雨情和受影响范围变化。"
    else:
        subject = _gis_alert_subject_line(d)
        code = (d.get("event_code") or "—").strip()
        para1 = f"当前应急响应已结束（事件：{subject}，编号：{code}），可进入历史复盘查询。"

    para2 = "您可以回答以下问题，进行进一步操作"
    bullets = "\n".join(_gis_alert_followup_lines(d))
    return f"{para1}\n\n{para2}\n\n{bullets}"


def _is_emergency_monitor_command(text: str) -> bool:
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t:
        return False
    has_emergency = ("应急响应" in t) or ("应急监测" in t)
    has_trigger = any(k in t for k in ("监测", "检测", "启动", "进行一次", "开始", "轮询", "查询", "查一下", "查下"))
    return has_emergency and has_trigger


def _is_stop_monitor_command(text: str) -> bool:
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t:
        return False
    return any(k in t for k in ("停止监测", "暂停监测", "结束监测", "取消监测", "不用监测", "停止轮询", "取消轮询"))


def _is_help_command(text: str) -> bool:
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t:
        return False
    if t in {"帮助", "菜单", "你能做什么", "我能问什么"}:
        return True
    return any(k in t for k in ("怎么用", "能问什么", "能做什么"))


def _help_menu_text() -> str:
    return "\n".join(
        [
            "你可以直接这样问（业务口语版）：",
            "",
            "【应急监测】",
            "- 查一下应急响应情况。",
            "- 做一次应急监测。",
            "- 持续监测应急响应（持续轮询）。",
            "- 停止监测。",
            "",
            "【发布确认】",
            "- 确认发布当前待处理的应急响应。",
            "- 暂不发布这条应急响应，备注待会商。",
            "",
            "【受影响范围】",
            "- 看看现在受影响的行政区有哪些。",
            "- 查一下今天上午8点受影响的行政区。",
            "- 看看可能受影响的河流水系有哪些。",
            "",
            "【专题图/图层】",
            "- 给我展示未来24、48、72小时累计降水专题图。",
            "- 当前可用的栅格图层有哪些？",
            "",
            "小提示：时间可以说“现在/今天上午8点/昨晚8点/过去24小时/未来三天”。",
        ]
    )


def _wants_continuous_monitor(text: str) -> bool:
    """
    业务语义：只有明确表达“持续/一直/轮询/每隔xx分钟”等才认为要持续监测。
    否则默认一次性“查询/监测一次”并结束，避免后台任务常驻。
    """
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t:
        return False
    return any(k in t for k in ("持续", "一直", "不停", "轮询", "每隔", "每", "自动监测", "持续监测"))


def _find_preferred_emergency_monitor_tool(tools):
    if not isinstance(tools, list):
        return None
    preferred = [
        "get_emergency_monitor_status",
        "poll_emergency_monitor_status",
        "query_emergency_monitor_status",
    ]
    for name in preferred:
        t = next((x for x in tools if getattr(x, "name", "") == name), None)
        if t is not None:
            return t
    # 这里不再做“模糊兜底匹配”，避免误命中需要必填参数的场景工具（如 fetch_emergency_http_scenario）。
    return None


def _load_emergency_monitor_tool_args() -> dict:
    raw = os.getenv("EMERGENCY_MONITOR_TOOL_ARGS", "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        print(f"[应急监测] EMERGENCY_MONITOR_TOOL_ARGS 解析失败：{e}")
    return {}


def _load_emergency_monitor_http_base_url() -> str:
    return os.getenv("EMERGENCY_MONITOR_BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/")


def _load_emergency_monitor_trigger_payload() -> dict:
    """
    触发监测请求体：
    - 可通过环境变量覆盖：EMERGENCY_MONITOR_TRIGGER_PAYLOAD='{"start_time":"2023073000", ...}'
    - 未配置时用空对象（兼容后端默认参数/工具兜底）
    """
    raw = os.getenv("EMERGENCY_MONITOR_TRIGGER_PAYLOAD", "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print(f"[应急监测] EMERGENCY_MONITOR_TRIGGER_PAYLOAD 解析失败：{e}")
        return {}


def _load_emergency_monitor_poll_params() -> dict:
    raw = os.getenv("EMERGENCY_MONITOR_POLL_PARAMS", "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print(f"[应急监测] EMERGENCY_MONITOR_POLL_PARAMS 解析失败：{e}")
        return {}


def _coerce_start_time_to_times(start_time: str) -> str | None:
    """
    将预报起报时次转为场景接口需要的 times（YYYYMMDDHH0000）。
    支持 YYYYMMDDHH / YYYYMMDDHHMMSS / YYYY-MM-DD HH[:MM[:SS]]。
    """
    if not start_time:
        return None
    text = str(start_time).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10}", text):
        return f"{text}0000"
    if re.fullmatch(r"\d{14}", text):
        return text
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y%m%d%H0000")
        except Exception:
            pass
    return None


def _build_emergency_regions_payload_from_trigger() -> dict:
    """
    轮询时用于拉取应急行政区图层的数据参数。
    可通过环境变量 EMERGENCY_MONITOR_REGIONS_PAYLOAD 完整覆盖。
    """
    raw = os.getenv("EMERGENCY_MONITOR_REGIONS_PAYLOAD", "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception as e:
            print(f"[应急监测] EMERGENCY_MONITOR_REGIONS_PAYLOAD 解析失败：{e}")
    trigger = _load_emergency_monitor_trigger_payload()
    start_time = str(trigger.get("start_time") or "").strip()
    times = _coerce_start_time_to_times(start_time)
    payload = {}
    if times:
        payload["times"] = times
    for k in ("scope", "basin_codes", "allowed_station_levels", "min_mm", "limit"):
        v = trigger.get(k)
        if v is not None and str(v) != "":
            payload[k] = v
    return payload


async def _http_post_json(url: str, payload: dict, timeout_sec: int = 60):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _call():
        req = Request(url=url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            try:
                err_raw = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_raw = ""
            raise RuntimeError(f"HTTP {e.code} POST {url} 失败，响应：{err_raw or e.reason}") from e

    return await asyncio.to_thread(_call)


async def _http_get_json(url: str, params: dict | None = None, timeout_sec: int = 60):
    full_url = url
    if params:
        q = urlencode({k: v for k, v in params.items() if v is not None and str(v) != ""})
        if q:
            full_url = f"{url}?{q}"

    def _call():
        req = Request(url=full_url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            try:
                err_raw = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_raw = ""
            raise RuntimeError(f"HTTP {e.code} GET {full_url} 失败，响应：{err_raw or e.reason}") from e

    return await asyncio.to_thread(_call)


def _extract_workflow_publish_from_board(payload: dict | None) -> dict:
    data = _unwrap_tool_result(payload)
    if not isinstance(data, dict):
        return {}
    qa = data.get("qa") if isinstance(data.get("qa"), dict) else {}
    wf = qa.get("workflow_publish") if isinstance(qa.get("workflow_publish"), dict) else {}
    return wf


def _format_workflow_publish_hint(wf: dict) -> str:
    pending = wf.get("pending_events") if isinstance(wf.get("pending_events"), list) else []
    if not pending:
        return ""
    lines = ["检测到待确认发布的应急响应："]
    for ev in pending[:5]:
        title = str(ev.get("title") or "应急响应").strip()
        level_txt = str(ev.get("level_display") or ev.get("event_level") or "").strip()
        phase_txt = str(ev.get("timeline_phase_display") or "").strip()
        tail = f"（{level_txt}）" if level_txt else ""
        phase = f"，当前阶段：{phase_txt}达到应急响应" if phase_txt else ""
        lines.append(
            f"- {title}{tail}{phase}"
        )

    lines.append("可回复：确认发布 / 暂不发布 。")
    return "\n".join(lines)


async def _maybe_push_workflow_publish_hint(board_res: dict | None):
    wf = _extract_workflow_publish_from_board(board_res if isinstance(board_res, dict) else {})
    step = str(wf.get("step") or "").strip().lower()
    pending = wf.get("pending_events") if isinstance(wf.get("pending_events"), list) else []
    if step != "confirm_publication" or not pending:
        # 无待确认事件时清理会话默认值，避免误确认旧事件
        cl.user_session.set("_wf_publish_default_ident", "")
        return
    # 有待确认事件时，默认回填首个事件标识，支持用户直接回复“确认发布”
    first_ident = ""
    first_ev = pending[0] if pending else {}
    if isinstance(first_ev, dict):
        for k in ("event_id", "id", "event_code"):
            v = first_ev.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                first_ident = s
                break
    cl.user_session.set("_wf_publish_default_ident", first_ident)
    key = "|".join(str((x or {}).get("event_id") or (x or {}).get("event_code") or "") for x in pending)
    prev_key = cl.user_session.get("_wf_publish_pending_key")
    if prev_key == key:
        return
    cl.user_session.set("_wf_publish_pending_key", key)
    hint = _format_workflow_publish_hint(wf)
    if hint:
        await cl.Message(content=hint, author="应急问答").send()


def _parse_publish_ack_command(text: str) -> dict | None:
    # 支持格式（业务口语 + 兼容技术口令）：
    # 1) 发布确认 22 已发布 备注xxx
    # 2) 确认发布 22 / 确认发布IV级应急响应
    # 3) 确认发布（使用当前选中时间轴事件）
    # 4) 不发布 / 暂不发布（使用当前选中时间轴事件）
    t = (text or "").strip()
    if not t:
        return None
    # 口语输入常带句号/逗号/语气词，先做轻量归一，避免“确认发布。”匹配失败
    t_simple = re.sub(r"[，,。.!！？?；;、]+", " ", t).strip()
    t_simple = re.sub(r"\s+", " ", t_simple)
    m = re.search(r"(发布确认|确认发布)\s+([A-Za-z0-9_\-]+)\s+(已发布|未发布)(?:\s+备注(.+))?$", t)
    if m:
        ident = m.group(2).strip()
        status_txt = m.group(3).strip()
        note = (m.group(4) or "").strip()
        return {"ident": ident, "published": status_txt == "已发布", "note": note, "source": "explicit"}

    # 简化口令：确认发布 22（默认已发布）
    m2 = re.search(r"(确认发布|发布确认)\s+([A-Za-z0-9_\-]+)(?:\s+备注(.+))?$", t_simple)
    if m2:
        ident = m2.group(2).strip()
        note = (m2.group(3) or "").strip()
        return {"ident": ident, "published": True, "note": note, "source": "explicit"}

    # 无 ID：依赖会话中的当前选中时间轴事件
    has_publish_confirm = ("确认发布" in t_simple or "发布确认" in t_simple)
    has_emergency_context = any(k in t_simple for k in ("应急响应", "应急", "级响应", "级应急"))
    if (has_publish_confirm or has_emergency_context) and ("未发布" not in t_simple and "不发布" not in t_simple):
        return {"ident": None, "published": True, "note": "", "source": "session"}
    if ("不发布" in t_simple) or ("未发布" in t_simple) or ("暂不发布" in t_simple):
        return {"ident": None, "published": False, "note": "", "source": "session"}
    return None


def _resolve_publish_ack_ident_from_session() -> str | None:
    """
    从最近一次前端时间轴选中事件中解析事件标识。
    优先 event_id，其次 id，最后 event_code。
    """
    selected = cl.user_session.get("last_gis_timeline_alert")
    if isinstance(selected, dict):
        for k in ("event_id", "id", "event_code"):
            v = selected.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
    # 回退：使用最近一次待确认事件默认标识（由 response-board 推送时写入）
    fallback_ident = cl.user_session.get("_wf_publish_default_ident")
    if fallback_ident is not None:
        s = str(fallback_ident).strip()
        if s:
            return s
    return None


async def _handle_publish_ack_command(text: str) -> bool:
    cmd = _parse_publish_ack_command(text)
    if not cmd:
        return False
    ident = str(cmd.get("ident") or "").strip()
    if not ident:
        ident = str(_resolve_publish_ack_ident_from_session() or "").strip()
    if not ident:
        await cl.Message(
            content=(
                "当前未识别到要确认发布的应急响应。"
                "请先在时间轴选择目标事件后再回复“确认发布”，"
                "或直接说“发布确认 事件编号 已发布”。"
            ),
            author="应急问答",
        ).send()
        return True

    base = _load_emergency_monitor_http_base_url()
    timeout_sec = max(5, int(os.getenv("EMERGENCY_MONITOR_HTTP_TIMEOUT_SEC", "60")))
    body = {"event_id": ident, "published": bool(cmd["published"])}
    if cmd["note"]:
        body["note"] = cmd["note"]
    try:
        res = await _http_post_json(f"{base}/emergency/management/workflow/publish-ack", body, timeout_sec=timeout_sec)
        ev = (res or {}).get("event") if isinstance(res, dict) else {}
        eid = ev.get("id") or ident
        st = "已发布" if cmd["published"] else "未发布"
        level_txt = str(ev.get("level_display") or ev.get("event_level") or "").strip()
        title_txt = str(ev.get("title") or "该应急响应").strip()
        if cmd["published"]:
            msg = f"已为你确认：{title_txt}（{level_txt or '响应'}）已正式发布。"
        else:
            msg = f"已为你记录：{title_txt}（{level_txt or '响应'}）暂不发布。"
        if cmd["note"]:
            msg += f" 备注：{cmd['note']}"
        await cl.Message(content=msg, author="应急问答").send()
        linkage_id = f"gis_emergency_workflow_{uuid4().hex[:10]}"
        packet = {
            "type": "gis_linkage",
            "schema_version": "v2",
            "linkage_id": linkage_id,
            "scene": "emergency_workflow",
            "scene_key": "scene.emergency_workflow",
            "query": text,
            "tool": {"name": "publish_ack_quick_command", "args": body},
            "meta": {
                "source": "chain_gzt",
                "emitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "route_key": "emergency_workflow:publish_ack_quick_command",
                "data_origin": "http_quick_command",
            },
            "map": {"geojson": None, "layers": [], "render_mode": "none"},
            "panel": {
                "tables": [
                    {
                        "table_key": "publish_ack",
                        "table_name": "发布确认",
                        "columns": [
                            {"key": "event_id", "title": "事件编号"},
                            {"key": "status", "title": "发布状态"},
                            {"key": "note", "title": "备注"},
                        ],
                        "rows": [{"event_id": eid, "status": st, "note": cmd["note"] or ""}],
                    }
                ]
            },
        }
        await _emit_gis_packet(packet)
    except Exception:
        await cl.Message(
            content="发布确认暂时未提交成功，请稍后重试；如持续失败，请联系值班技术支持。",
            author="应急问答",
        ).send()
    return True


async def _send_calculating_hint(scene: str) -> None:
    """
    耗时后端调用前给前端一个即时反馈，避免用户等待无感知。
    """
    label = (scene or "").strip() or "当前任务"
    await cl.Message(content=f"{label}正在计算，请稍候…", author="GIS问答").send()


def _business_query_failed_text(scene: str) -> str:
    scene_txt = (scene or "").strip() or "当前查询"
    return f"{scene_txt}暂时未取到可用结果。请先确认时间范围后重试；若持续失败，请联系值班技术支持。"


def _business_no_data_text(scene: str) -> str:
    scene_txt = (scene or "").strip() or "当前查询"
    return f"暂未查询到可展示的{scene_txt}。可换一个时间点，或先查看最新一次监测结果。"


async def _handle_gis_quick_command(text: str, tools=None) -> bool:
    done_text = "已为您在地图中展示结果。"
    t = (text or "").strip().lower()
    base = _load_emergency_monitor_http_base_url()
    timeout_sec = max(5, int(os.getenv("EMERGENCY_MONITOR_HTTP_TIMEOUT_SEC", "60")))
    times_14 = _parse_common_time_to_14(text)
    if not times_14:
        # 业务口语常不含明确时次，尽量复用最近一次成功解析的时间（仅作兜底，不强行覆盖明确输入）
        fallback_times = str(cl.user_session.get("last_times_14") or "").strip()
        if fallback_times:
            times_14 = fallback_times

    def _impact_mode(raw_text: str) -> str:
        s = (raw_text or "").strip()
        if any(k in s for k in ("实况影响", "实况", "观测影响", "观测")):
            return "observation"
        if any(k in s for k in ("预报影响", "预报", "未来")):
            return "forecast"
        # 默认按预报口径，兼容既有问法
        return "forecast"

    impact_mode = _impact_mode(text)

    def _has_map_or_table(tool_name: str, observation, scene: str) -> bool:
        try:
            geojson, panel_tables, _, _ = _extract_geojson_and_tables(tool_name, observation, scene)
            if isinstance(geojson, dict) and isinstance(geojson.get("features"), list) and geojson.get("features"):
                return True
            if isinstance(panel_tables, list):
                for tb in panel_tables:
                    if isinstance(tb, dict) and isinstance(tb.get("rows"), list) and tb.get("rows"):
                        return True
            return False
        except Exception:
            return False

    # 老场景：国家站点观测（带历史时次），兼容业务化表达
    wants_station_obs = (
        ("国家站" in text and any(k in text for k in ("观测", "监测", "站点")))
        or ("站点观测" in text)
        or ("站点监测" in text)
    )
    if wants_station_obs and times_14:
        cl.user_session.set("last_times_14", times_14)
        params = {"times": times_14, "scope": "haihe"}
        try:
            data = await _http_get_json(f"{base}/scenario/monitor/stations", params=params, timeout_sec=timeout_sec)
            if not _has_map_or_table("monitor_stations_quick_command", data, "realtime_station"):
                raise RuntimeError("monitor/stations 返回为空")
            await cl.Message(
                content=done_text,
                author="GIS问答",
            ).send()
            await _send_gis_linkage(
                tool_name="monitor_stations_quick_command",
                tool_args=params,
                observation=data,
                user_text=f"国家站点观测 {times_14}",
                tools=None,
            )
        except Exception as e:
            # 兜底到本地 monitor.json
            print(f"[GIS问答] 国家站点观测实时查询失败，走本地兜底：{e}")
            local_payload = _load_local_scene_export("realtime_station", {"times": times_14}, f"国家站点观测 {times_14}")
            if not _has_map_or_table("local_scene_export", local_payload or {}, "realtime_station"):
                await cl.Message(
                    content=_business_no_data_text("国家站点观测"),
                    author="GIS问答",
                ).send()
                return True
            await _send_gis_linkage(
                tool_name="local_scene_export_probe",
                tool_args={"times": times_14},
                observation={},
                user_text=f"国家站点观测 {times_14}",
                tools=None,
            )
            await cl.Message(
                content=done_text,
                author="GIS问答",
            ).send()
        return True

    # 老场景：应急影响河流（带时次），兼容业务化表达
    wants_emergency_rivers = (
        ("应急影响" in text and any(k in text for k in ("河流", "河系", "河道")))
        or ("受影响河流" in text)
        or ("受影响河系" in text)
        or ("可能影响的河流" in text)
    )
    if wants_emergency_rivers and times_14:
        cl.user_session.set("last_times_14", times_14)
        river_name = ""
        # 优先识别用户显式输入的“xx河”（避免把整句“...影响的河”误当河名）
        candidates = re.findall(r"([\u4e00-\u9fa5]{1,6}河)", text)
        if candidates:
            # 取最后一个候选，通常更接近用户真正指定的河名
            bad_tokens = ("帮我", "查看", "看看", "可能", "影响", "受影响", "河流", "河系", "哪些", "一下")
            for c in reversed(candidates):
                c_strip = c.strip()
                if any(tok in c_strip for tok in bad_tokens):
                    continue
                if len(c_strip) <= 1:
                    continue
                river_name = c_strip
                break
        # 兼容既有常见河名兜底
        if not river_name:
            if "永定河" in text:
                river_name = "永定河"
            elif "海河" in text:
                river_name = "海河"
        # 受影响河流场景固定优先走 wms_sql（只回传 sql_id，避免大 GeoJSON 导致前端 message handler 卡顿）
        # 本地联调默认 force_local=1（可通过环境变量 EMERGENCY_RIVERS_FORCE_LOCAL=0 关闭）
        force_local_default = str(os.getenv("EMERGENCY_RIVERS_FORCE_LOCAL", "1")).strip().lower() not in {"0", "false", "no", "off"}
        params = {"times": times_14, "scope": "haihe", "map_render": "wms_sql"}
        if force_local_default:
            params["force_local"] = "1"
        if river_name:
            cl.user_session.set("last_river_name", river_name)
            params["river_name"] = river_name
        await _send_calculating_hint("受影响河系分析")
        try:
            data = await _http_get_json(f"{base}/scenario/emergency/rivers", params=params, timeout_sec=timeout_sec)
            if not _has_map_or_table("emergency_rivers_quick_command", data, "emergency_rivers"):
                raise RuntimeError("emergency/rivers 返回为空")
            await cl.Message(
                content=done_text,
                author="GIS问答",
            ).send()
            await _send_gis_linkage(
                tool_name=f"emergency_rivers_quick_command_{impact_mode}",
                tool_args=params,
                observation=data,
                user_text=f"{'实况影响' if impact_mode == 'observation' else '预报影响'}河流 {times_14} {river_name or '降雨驱动'}",
                tools=None,
            )
        except Exception as e:
            # 轻量兜底：仍走后端 /scenario/emergency/rivers，但强制 local + wms_sql，
            # 不再回落到 local_scene_export_probe（该路径可能携带超大 GeoJSON）。
            print(f"[GIS问答] 应急影响河流实时查询失败，尝试 force_local+wms_sql 兜底：{e}")
            try:
                fallback_params = dict(params)
                fallback_params["force_local"] = "1"
                data_local = await _http_get_json(
                    f"{base}/scenario/emergency/rivers",
                    params=fallback_params,
                    timeout_sec=timeout_sec,
                )
                if not _has_map_or_table("emergency_rivers_quick_command", data_local, "emergency_rivers"):
                    raise RuntimeError("emergency/rivers force_local 返回为空")
                await _send_gis_linkage(
                    tool_name=f"emergency_rivers_quick_command_{impact_mode}",
                    tool_args=fallback_params,
                    observation=data_local,
                    user_text=f"{'实况影响' if impact_mode == 'observation' else '预报影响'}河流 {times_14} {river_name or '降雨驱动'}(local)",
                    tools=None,
                )
                await cl.Message(content=done_text, author="GIS问答").send()
            except Exception as e2:
                print(f"[GIS问答] force_local+wms_sql 兜底失败：{e2}")
                await cl.Message(
                    content=_business_no_data_text("应急影响河流"),
                    author="GIS问答",
                ).send()
        return True

    # 老场景：应急影响分区/行政区（带时次），兼容业务化表达
    wants_emergency_regions = (
        ("应急影响" in text and any(k in text for k in ("分区", "行政区", "行政区划")))
        or ("受影响行政区" in text)
        or ("受影响分区" in text)
        or ("影响范围" in text and any(k in text for k in ("行政区", "分区")))
    )
    if wants_emergency_regions and times_14:
        cl.user_session.set("last_times_14", times_14)
        # 两类都拉，前端可按 scene 分开渲染
        reg_params = {"times": times_14, "scope": "haihe", "map_render": "wms_sql"}
        part_params = {"times": times_14, "scope": "haihe", "map_render": "wms_sql"}
        ok_any = False
        await _send_calculating_hint("应急影响分区与行政区")
        try:
            reg_data = await _http_get_json(f"{base}/scenario/emergency/regions", params=reg_params, timeout_sec=timeout_sec)
            if _has_map_or_table("emergency_regions_quick_command", reg_data, "emergency_districts"):
                await _send_gis_linkage(
                    tool_name=f"emergency_regions_quick_command_{impact_mode}",
                    tool_args=reg_params,
                    observation=reg_data,
                    user_text=f"{'实况影响' if impact_mode == 'observation' else '预报影响'} 行政区划 {times_14}",
                    tools=None,
                )
                ok_any = True
        except Exception as e:
            print(f"[GIS问答] regions 查询失败，走本地兜底：{e}")
            local_payload = _load_local_scene_export("emergency_districts", {"times": times_14}, f"应急影响 行政区划 {times_14}")
            if _has_map_or_table("local_scene_export", local_payload or {}, "emergency_districts"):
                await _send_gis_linkage(
                    tool_name="local_scene_export_probe",
                    tool_args={"times": times_14},
                    observation={},
                    user_text=f"应急影响 行政区划 {times_14}",
                    tools=None,
                )
                ok_any = True
        try:
            part_data = await _http_get_json(f"{base}/scenario/emergency/partitions", params=part_params, timeout_sec=timeout_sec)
            if _has_map_or_table("emergency_partitions_quick_command", part_data, "emergency_partitions"):
                await _send_gis_linkage(
                    tool_name=f"emergency_partitions_quick_command_{impact_mode}",
                    tool_args=part_params,
                    observation=part_data,
                    user_text=f"{'实况影响' if impact_mode == 'observation' else '预报影响'} 分区 {times_14}",
                    tools=None,
                )
                ok_any = True
        except Exception as e:
            print(f"[GIS问答] partitions 查询失败，走本地兜底：{e}")
            local_payload = _load_local_scene_export("emergency_partitions", {"times": times_14}, f"应急影响 分区 {times_14}")
            if _has_map_or_table("local_scene_export", local_payload or {}, "emergency_partitions"):
                await _send_gis_linkage(
                    tool_name="local_scene_export_probe",
                    tool_args={"times": times_14},
                    observation={},
                    user_text=f"应急影响 分区 {times_14}",
                    tools=None,
                )
                ok_any = True
        if ok_any:
            await cl.Message(content=done_text, author="GIS问答").send()
            return True
        await cl.Message(content=_business_no_data_text("应急影响分区或行政区"), author="GIS问答").send()
        return True

    # 业务化表达拆分：
    # 1) 预报影响降水（GIS 联用）
    # 2) 专题图（聊天框出图）
    wants_rain = any(k in text for k in ("面雨量", "累计降水", "降水", "降雨"))
    wants_thematic = any(k in text for k in ("专题图", "降雨专题图", "降雨分布图", "生成图", "生成专题图"))
    wants_forecast = any(k in text for k in ("未来", "预计", "预报", "未来三天", "24小时", "48小时", "72小时"))
    wants_observation = any(k in text for k in ("实况", "观测", "过去", "近24小时", "近48小时", "已发生"))
    hour_hits = []
    # 兼容“24、48、72小时”这类并列写法（只有最后一个带“小时”）
    for m in re.finditer(
        r"((?:12|24|36|48|60|72)(?:\s*[、,，/]\s*(?:12|24|36|48|60|72))*)\s*小时",
        text,
    ):
        hour_hits.extend(re.findall(r"(12|24|36|48|60|72)", m.group(1)))
    # 兼容英文写法（24h/48h/72h）
    if not hour_hits:
        hour_hits = re.findall(r"(12|24|36|48|60|72)\s*h\b", t)
    if (wants_rain and not wants_thematic) and (wants_forecast or wants_observation):
        is_obs_mode = impact_mode == "observation" or (wants_observation and not wants_forecast)
        req_hours = sorted({int(h) for h in hour_hits}) if hour_hits else ([12, 24, 36, 48] if is_obs_mode else [24, 48, 72])
        req_ids = {f"{'obs' if is_obs_mode else 'fcst'}_cumulative_{h}h" for h in req_hours}
        await _send_calculating_hint("降水图层")
        try:
            data = await _http_get_json(f"{base}/emergency/gis/geoserver-layers", timeout_sec=timeout_sec)
            all_layers = data.get("layers") if isinstance(data, dict) else []
            selected = []
            if isinstance(all_layers, list):
                for x in all_layers:
                    if isinstance(x, dict) and str(x.get("id") or "") in req_ids:
                        selected.append(x)
            selected.sort(key=lambda x: int(re.search(r"(\d+)h$", str(x.get("id") or "")).group(1)) if re.search(r"(\d+)h$", str(x.get("id") or "")) else 999)
            if not selected:
                await cl.Message(
                    content=(
                        f"未找到{','.join(str(h) for h in req_hours)}小时对应的"
                        f"{'实况' if is_obs_mode else '预报'}累计降水图层，请先确认 GeoServer 已发布。"
                    ),
                    author="GIS问答",
                ).send()
                return True
            await cl.Message(content=done_text, author="GIS问答").send()
            if times_14:
                cl.user_session.set("last_times_14", times_14)

            intensity_table = None
            start_compact = ""
            start_time_source = ""
            if not is_obs_mode:
                start_compact = _ec_forecast_start_compact_from_times14(times_14)
                if start_compact:
                    start_time_source = "question_or_session"
                else:
                    # 问句未带可解析时次且无 last_times_14 时：本地联调仍去读 ecOutput 目录，
                    # 起报用当前时间并归一到 EC 合法时次（与 _normalize_forecast_start_time_10 一致）。
                    start_compact = _normalize_forecast_start_time_10(datetime.now().strftime("%Y%m%d%H"))
                    start_time_source = "default_now_local"
            if not is_obs_mode and start_compact:
                try:
                    stats_data = await _http_get_json(
                        f"{base}/emergency/gis/forecast-precip-intensity-stats",
                        params={
                            "raster_source": "geoserver",
                            "start_time_compact": start_compact,
                            "lead_hours": ",".join(str(h) for h in req_hours),
                        },
                        timeout_sec=timeout_sec,
                    )
                    if isinstance(stats_data, dict) and isinstance(stats_data.get("rows"), list):
                        def _fmt2(v):
                            fv = _to_float(v)
                            return round(fv, 2) if fv is not None else None

                        rows_out = []
                        for r in stats_data["rows"]:
                            if not isinstance(r, dict):
                                continue
                            rows_out.append(
                                {
                                    "lead_hours": r.get("lead_hours"),
                                    "extreme_pct": _fmt2(r.get("extreme_storm_area_pct")),
                                    "heavy_pct": _fmt2(r.get("heavy_storm_area_pct")),
                                    "storm_pct": _fmt2(r.get("storm_area_pct")),
                                    "basin_km2": _fmt2(r.get("basin_valid_area_km2_approx")),
                                }
                            )
                        intensity_table = {
                            "table_key": "forecast_precip_basin_intensity",
                            "table_name": "海河流域预报降水强度面积占比",
                            "columns": [
                                {"key": "lead_hours", "title": "预报时效(h)"},
                                {"key": "extreme_pct", "title": "特大暴雨面积占比(%)"},
                                {"key": "heavy_pct", "title": "大暴雨面积占比(%)"},
                                {"key": "storm_pct", "title": "暴雨面积占比(%)"},
                                {"key": "basin_km2", "title": "流域有效面积(km²)"},
                            ],
                            "rows": rows_out,
                        }
                except Exception as exc:
                    print(f"[GIS问答] 预报降水强度面积统计失败: {exc}")

            meta = {
                "source": "chain_gzt",
                "emitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "route_key": "rainfall_layers_observation:quick_command" if is_obs_mode else "rainfall_layers_forecast:quick_command",
                "data_origin": "http_quick_command",
            }
            if not is_obs_mode and start_compact:
                meta["forecast_precip_intensity"] = {
                    "raster_source": "geoserver",
                    "ref_start_time_compact": start_compact,
                    "lead_hours": req_hours,
                    "start_time_source": start_time_source or "unknown",
                    "note": (
                        "强度占比与地图 WMS 同源：后端经 GeoServer WCS GetCoverage 拉取 "
                        "[geoserver] layer_fcst_cumulative_*h 对应的 coverage，在海河流域 boundary_shp 内统计；"
                        "本地联调无需对齐 ec_*_rain_total_*.tif 文件名。"
                    )
                    + (
                        "（ref_start_time_compact 仅作文档参考/兼容旧参数；WCS 不依赖该起报。）"
                        if start_time_source == "default_now_local"
                        else ""
                    ),
                }

            panel_tables = []
            if intensity_table:
                # 仅展示面积统计表（实况/预报都不再展示图层清单表）。
                panel_tables.append(intensity_table)

            packet = {
                "type": "gis_linkage",
                "schema_version": "v2",
                "linkage_id": f"gis_rainfall_layers_{uuid4().hex[:10]}",
                "scene": "rainfall_layers_observation" if is_obs_mode else "rainfall_layers_forecast",
                "scene_key": "scene.rainfall_layers_observation" if is_obs_mode else "scene.rainfall_layers_forecast",
                "query": text,
                "tool": {
                    "name": "rainfall_layers_quick_command_observation" if is_obs_mode else "rainfall_layers_quick_command_forecast",
                    "args": {"hours": req_hours, "kind": "observation" if is_obs_mode else "forecast"},
                },
                "meta": meta,
                "map": {"render_mode": "wms_catalog", "wms_layers": selected, "geojson": None, "layers": []},
                "panel": {"tables": panel_tables},
            }
            await _emit_gis_packet(packet)
        except Exception:
            await cl.Message(content=_business_query_failed_text("降水图层"), author="GIS问答").send()
        return True
    if wants_thematic and (wants_forecast or wants_observation):
        is_obs_mode = impact_mode == "observation" or (wants_observation and not wants_forecast)
        req_hours = sorted({int(h) for h in hour_hits}) if hour_hits else ([12, 24, 36, 48] if is_obs_mode else [24, 48, 72])
        # 专题图分支固定只在聊天框输出，不再做 GIS 联用包
        wants_gis_linkage = False
        await _send_calculating_hint("降雨专题图层")
        try:
            # A) GIS 联动：GeoServer 栅格图层
            selected = []
            if wants_gis_linkage:
                data = await _http_get_json(f"{base}/emergency/gis/geoserver-layers", timeout_sec=timeout_sec)
                all_layers = data.get("layers") if isinstance(data, dict) else []
                if isinstance(all_layers, list):
                    for x in all_layers:
                        if isinstance(x, dict) and str(x.get("id") or "") in req_ids:
                            selected.append(x)
                selected.sort(key=lambda x: int(re.search(r"(\d+)h$", str(x.get("id") or "")).group(1)) if re.search(r"(\d+)h$", str(x.get("id") or "")) else 999)
                if not selected:
                    await cl.Message(
                        content=(
                            f"未找到{','.join(str(h) for h in req_hours)}小时对应的"
                            f"{'实况' if is_obs_mode else '预报'}累计降水图层，请先确认 GeoServer 已发布。"
                        ),
                        author="GIS问答",
                    ).send()
                    return True

            # B) 专题图生成：MCP 制图工具（与 GeoServer 联动并行存在）
            thematic_job_payload = None
            thematic_source_time = ""
            thematic_source_prefix = ""
            candidate_tools = tools if isinstance(tools, list) else cl.user_session.get("tools")
            if isinstance(candidate_tools, list):
                mcp_tool_name = (
                    "create_haihe_observation_impact_precip_map_job"
                    if is_obs_mode
                    else "create_haihe_forecast_impact_precip_map_job"
                )
                mcp_tool = next((x for x in candidate_tools if getattr(x, "name", "") == mcp_tool_name), None)
                if mcp_tool is not None:
                    try:
                        effective_times = times_14 or datetime.now().strftime("%Y%m%d%H0000")
                        if is_obs_mode:
                            thematic_source_time = effective_times
                            thematic_source_prefix = "haihe_obs_idw"
                            thematic_job_payload = await mcp_tool.ainvoke(
                                {
                                    "times": effective_times,
                                    "accum_hours": ",".join(str(h) for h in req_hours),
                                }
                            )
                        else:
                            # 预报工具口径以起报时次为主；传 YYYYMMDDHH 最稳
                            start_time_raw = (effective_times[:10] if len(effective_times) >= 10 else effective_times)
                            start_time = _normalize_forecast_start_time_10(start_time_raw)
                            ec_output_path_local = _load_local_ec_output_path_for_debug()
                            latest_start = _pick_latest_available_forecast_start_time_10(ec_output_path_local, req_hours)
                            if latest_start and latest_start != start_time:
                                start_time = latest_start
                            thematic_source_time = start_time
                            thematic_source_prefix = f"ec_{start_time}_rain_total"
                            thematic_job_payload = await mcp_tool.ainvoke(
                                {
                                    "start_time": start_time,
                                    "hours": ",".join(str(h) for h in req_hours),
                                }
                            )
                    except Exception as tool_err:
                        thematic_job_payload = {"error": str(tool_err), "tool_name": mcp_tool_name}

            # MCP 工具不可用时，兜底走后端 HTTP 任务接口，保证“专题图任务”结果可回传前端控制台。
            if not isinstance(thematic_job_payload, dict):
                try:
                    effective_times = times_14 or datetime.now().strftime("%Y%m%d%H0000")
                    if is_obs_mode:
                        thematic_source_time = effective_times
                        thematic_source_prefix = "haihe_obs_idw"
                        job_res = await _http_post_json(
                            f"{base}/emergency/observation/products/jobs",
                            {
                                "times": effective_times,
                                "accum_hours": ",".join(str(h) for h in req_hours),
                            },
                            timeout_sec=timeout_sec,
                        )
                        thematic_job_payload = {
                            "message": "已为您创建实况影响降水专题图任务。",
                            "job": job_res if isinstance(job_res, dict) else {},
                            "tool_name": "http:POST /emergency/observation/products/jobs",
                        }
                    else:
                        start_time_raw = (effective_times[:10] if len(effective_times) >= 10 else effective_times)
                        start_time = _normalize_forecast_start_time_10(start_time_raw)
                        ec_output_path_local = _load_local_ec_output_path_for_debug()
                        latest_start = _pick_latest_available_forecast_start_time_10(ec_output_path_local, req_hours)
                        if latest_start and latest_start != start_time:
                            start_time = latest_start
                        thematic_source_time = start_time
                        thematic_source_prefix = f"ec_{start_time}_rain_total"
                        job_res = await _http_post_json(
                            f"{base}/emergency/forecast/products/jobs",
                            {
                                "start_time": start_time,
                                "hours": ",".join(str(h) for h in req_hours),
                            },
                            timeout_sec=timeout_sec,
                        )
                        thematic_job_payload = {
                            "message": "已为您创建预报影响降水专题图任务。",
                            "job": job_res if isinstance(job_res, dict) else {},
                            "tool_name": "http:POST /emergency/forecast/products/jobs",
                        }
                except Exception as http_job_err:
                    thematic_job_payload = {
                        "error": str(http_job_err),
                        "tool_name": "http_fallback",
                        "message": "专题图任务暂时创建失败，请稍后重试。",
                    }

            packet = None
            if wants_gis_linkage:
                linkage_id = f"gis_rainfall_thematic_{uuid4().hex[:10]}"
                packet = {
                    "type": "gis_linkage",
                    "schema_version": "v2",
                    "linkage_id": linkage_id,
                    "scene": "rainfall_thematic_map_observation" if is_obs_mode else "rainfall_thematic_map_forecast",
                    "scene_key": "scene.rainfall_thematic_map_observation" if is_obs_mode else "scene.rainfall_thematic_map_forecast",
                    "query": text,
                    "tool": {
                        "name": "rainfall_thematic_quick_command_observation" if is_obs_mode else "rainfall_thematic_quick_command_forecast",
                        "args": {"hours": req_hours, "kind": "observation" if is_obs_mode else "forecast"},
                    },
                    "meta": {
                        "source": "chain_gzt",
                        "emitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "route_key": "rainfall_thematic_map_observation:quick_command" if is_obs_mode else "rainfall_thematic_map_forecast:quick_command",
                        "data_origin": "http_quick_command+mcp_tool",
                        "source_time_compact": thematic_source_time,
                        "source_file_prefix": thematic_source_prefix,
                    },
                    "map": {
                        "render_mode": "wms_catalog",
                        "wms_layers": selected,
                        "geojson": None,
                        "layers": [],
                    },
                    "panel": {
                        "tables": [
                            {
                                "table_key": "forecast_cumulative_rainfall_layers",
                                "table_name": "实况累计降水专题图层" if is_obs_mode else "预报累计降水专题图层",
                                "columns": [
                                    {"key": "id", "title": "图层ID"},
                                    {"key": "title", "title": "图层名称"},
                                    {"key": "layer", "title": "GeoServer图层"},
                                    {"key": "kind", "title": "类型"},
                                ],
                                "rows": selected,
                            }
                        ]
                    },
                }
            if isinstance(thematic_job_payload, dict):
                job_info = thematic_job_payload.get("job") if isinstance(thematic_job_payload.get("job"), dict) else {}
                if isinstance(packet, dict):
                    packet["panel"]["tables"].append(
                        {
                            "table_key": "thematic_generation_job",
                            "table_name": "专题图生成任务",
                            "columns": [
                                {"key": "mode", "title": "口径"},
                                {"key": "method", "title": "生成方式"},
                                {"key": "source_time", "title": "使用时次"},
                                {"key": "source_prefix", "title": "文件前缀"},
                                {"key": "job_id", "title": "任务编号"},
                                {"key": "status_display", "title": "任务状态"},
                                {"key": "message", "title": "结果"},
                            ],
                            "rows": [
                                {
                                    "mode": "实况影响" if is_obs_mode else "预报影响",
                                    "method": "系统自动生成",
                                    "source_time": thematic_source_time,
                                    "source_prefix": thematic_source_prefix,
                                    "job_id": job_info.get("job_id") or job_info.get("id"),
                                    "status_display": (
                                        "生成失败"
                                        if thematic_job_payload.get("error")
                                        else ("生成中" if (job_info.get("status") or "").strip().lower() in {"queued", "running", "pending"} else "已创建")
                                    ),
                                    "message": thematic_job_payload.get("message") or "专题图任务已提交。",
                                }
                            ],
                        }
                    )
                # 创建成功后异步轮询任务状态，完成后自动回推“专题图结果”到前端控制台。
                job_id = str(job_info.get("job_id") or job_info.get("id") or "").strip()
                if job_id:
                    _start_thematic_watch_task(
                        base_url=base,
                        timeout_sec=timeout_sec,
                        is_obs_mode=is_obs_mode,
                        job_id=job_id,
                        req_hours=req_hours,
                        query_text=text,
                        emit_gis_packet=wants_gis_linkage,
                    )
                else:
                    await cl.Message(content="专题图任务提交成功但未返回任务编号，暂无法自动回传图片。", author="GIS问答").send()
            if isinstance(packet, dict):
                await _emit_gis_packet(packet)
        except Exception:
            await cl.Message(content=_business_query_failed_text("降雨专题图层"), author="GIS问答").send()
        return True

    if (
        ("栅格" in text and "列表" in text)
        or any(k in text for k in ("可用栅格", "可用图层", "栅格图层", "有哪些栅格", "可展示栅格"))
        or ("geoserver" in t and "layer" in t)
    ):
        await _send_calculating_hint("栅格图层列表")
        try:
            data = await _http_get_json(f"{base}/emergency/gis/geoserver-layers", timeout_sec=timeout_sec)
            layers = data.get("layers") if isinstance(data, dict) else []
            lines = [f"GeoServer 图层数量：{len(layers) if isinstance(layers, list) else 0}"]
            if isinstance(layers, list):
                for x in layers[:12]:
                    lines.append(f"- {x.get('id')}: {x.get('layer')}")
            await cl.Message(content=done_text, author="GIS问答").send()
            linkage_id = f"gis_geoserver_layers_{uuid4().hex[:10]}"
            table_rows = []
            if isinstance(layers, list):
                for x in layers:
                    if isinstance(x, dict):
                        table_rows.append(
                            {
                                "id": x.get("id"),
                                "layer": x.get("layer"),
                                "label": x.get("label"),
                                "product_type": x.get("product_type"),
                            }
                        )
            packet = {
                "type": "gis_linkage",
                "schema_version": "v2",
                "linkage_id": linkage_id,
                "scene": "geoserver_layers",
                "scene_key": "scene.geoserver_layers",
                "query": text,
                "tool": {"name": "geoserver_layers_quick_command", "args": {}},
                "meta": {
                    "source": "chain_gzt",
                    "emitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "route_key": "geoserver_layers:quick_command",
                    "data_origin": "http_quick_command",
                },
                "map": {"geojson": None, "layers": [], "render_mode": "none"},
                "panel": {
                    "tables": [
                        {
                            "table_key": "geoserver_layers",
                            "table_name": "GeoServer 栅格图层",
                            "columns": [
                                {"key": "id", "title": "图层ID"},
                                {"key": "layer", "title": "图层名"},
                                {"key": "label", "title": "展示名"},
                                {"key": "product_type", "title": "类型"},
                            ],
                            "rows": table_rows,
                        }
                    ]
                },
            }
            await _emit_gis_packet(packet)
        except Exception:
            await cl.Message(content=_business_query_failed_text("栅格图层列表"), author="GIS问答").send()
        return True
    # 业务化表达：查看受影响河流水系
    # - 指定“246分区/分区河系”时：走 zone256-rivers（落区河系统计）
    # - 其他“受影响河流/河系”语义：走 emergency/rivers（直接/间接影响河流）
    m = re.search(
        r"(246分区河系|分区河系|受影响河系|河系图|影响河流水系|影响河道|可能影响的河流|受影响的河流)\s*(\d{10,14})?",
        text,
    )
    if m:
        phrase = str(m.group(1) or "").strip()
        times = (m.group(2) or "").strip()
        if not times:
            mt = re.search(r"\b(\d{10,14})\b", text)
            if mt:
                times = mt.group(1).strip()
        if not times:
            last_times = cl.user_session.get("last_times_14")
            if isinstance(last_times, list) and last_times:
                cand = str(last_times[0] or "").strip()
                if re.fullmatch(r"\d{14}", cand):
                    times = cand
            elif isinstance(last_times, str):
                cand = last_times.strip()
                if re.fullmatch(r"\d{14}", cand):
                    times = cand
        if len(times) == 10:
            times = f"{times}0000"
        if not times:
            # 业务化兜底：无明确时间时，按当前整点自动查询，避免让用户补技术参数。
            now = datetime.now().replace(minute=0, second=0, microsecond=0)
            times = now.strftime("%Y%m%d%H0000")
            cl.user_session.set("last_times_14", [times])
        use_zone256 = ("246分区" in phrase) or ("分区河系" in phrase)
        # 仅在用户明确指定河名时才传 river_name；
        # 未指定时交给后端按降雨落区自动推断“实际受影响河流”，避免默认海河造成偏差。
        river_name = ""
        if "永定河" in text:
            river_name = "永定河"
        elif "海河" in text:
            river_name = "海河"
        params = (
            {"times": times, "scope": "haihe", "map_render": "wms_sql"}
            if use_zone256
            else (
                {"times": times, "scope": "haihe", "river_name": river_name, "map_render": "wms_sql"}
                if river_name
                else {"times": times, "scope": "haihe", "map_render": "wms_sql"}
            )
        )
        await _send_calculating_hint("受影响河系分析")
        try:
            endpoint = "/scenario/emergency/zone256-rivers" if use_zone256 else "/scenario/emergency/rivers"
            data = await _http_get_json(f"{base}{endpoint}", params=params, timeout_sec=timeout_sec)
            await cl.Message(content=done_text, author="GIS问答").send()
            await _send_gis_linkage(
                tool_name=(
                    f"zone256_rivers_quick_command_{impact_mode}"
                    if use_zone256
                    else f"emergency_rivers_quick_command_{impact_mode}"
                ),
                tool_args=params,
                observation=data,
                user_text=f"{'实况影响' if impact_mode == 'observation' else '预报影响'} {text}",
                tools=None,
            )
            await cl.Message(content="已为您展示河系分析结果。", author="GIS问答").send()
        except Exception:
            await cl.Message(content=_business_query_failed_text("受影响河系分析"), author="GIS问答").send()
        return True
    return False


def _extract_monitor_status_line_from_response_board(payload) -> str:
    data = _unwrap_tool_result(payload)
    if not isinstance(data, dict):
        return "状态已更新"
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    timeline = data.get("timeline") if isinstance(data.get("timeline"), dict) else {}
    event_count = timeline.get("event_count")
    if isinstance(event_count, int):
        past_n = len(groups.get("past", []) or [])
        now_n = len(groups.get("now", []) or [])
        ongoing_n = len(groups.get("ongoing", []) or [])
        future_n = len(groups.get("future_hours", []) or [])
        return (
            f"本次共识别到 {event_count} 条应急事件，"
            f"其中过去 {past_n} 条、当前 {now_n} 条、进行中 {ongoing_n} 条、未来 {future_n} 条。"
        )
    return "状态已更新"


def _is_emergency_monitor_verbose_updates_enabled() -> bool:
    return os.getenv("EMERGENCY_MONITOR_VERBOSE_UPDATES", "0").strip().lower() in {"1", "true", "yes", "on"}


async def _run_emergency_monitor_http_round(user_text: str, round_idx: int):
    base = _load_emergency_monitor_http_base_url()
    timeout_sec = max(5, int(os.getenv("EMERGENCY_MONITOR_HTTP_TIMEOUT_SEC", "60")))
    trigger_path = os.getenv("EMERGENCY_MONITOR_TRIGGER_PATH", "/emergency/forecast").strip() or "/emergency/forecast"
    poll_path = os.getenv("EMERGENCY_MONITOR_POLL_PATH", "/emergency/management/response-board").strip() or "/emergency/management/response-board"

    # 第一次轮询前先触发一次检测（满足“指令后立即开始检测”）
    if round_idx == 1:
        trigger_payload = _load_emergency_monitor_trigger_payload()
        trigger_res = await _http_post_json(f"{base}{trigger_path}", trigger_payload, timeout_sec=timeout_sec)
        trigger_status = _extract_emergency_monitor_status_line(trigger_res)
        if _is_emergency_monitor_verbose_updates_enabled():
            await cl.Message(content=f"已发起一次应急监测：{trigger_status}", author="应急监测").send()

    poll_params = _load_emergency_monitor_poll_params()
    board_res = await _http_get_json(f"{base}{poll_path}", params=poll_params, timeout_sec=timeout_sec)
    if _is_emergency_monitor_verbose_updates_enabled():
        status_line = _extract_monitor_status_line_from_response_board(board_res)
        await cl.Message(content=f"第 {round_idx} 次应急监测已更新：{status_line}", author="应急监测").send()
    # 自动提示“待确认发布”问答步骤
    await _maybe_push_workflow_publish_hint(board_res if isinstance(board_res, dict) else None)
    enable_regions = os.getenv("EMERGENCY_MONITOR_ENABLE_REGIONS", "0").strip().lower() in {"1", "true", "yes", "on"}
    regions_path = os.getenv("EMERGENCY_MONITOR_REGIONS_PATH", "/scenario/emergency/regions").strip() or "/scenario/emergency/regions"
    regions_payload = _build_emergency_regions_payload_from_trigger()
    regions_res = None
    if enable_regions and regions_payload.get("times"):
        try:
            regions_res = await _http_post_json(f"{base}{regions_path}", regions_payload, timeout_sec=timeout_sec)
        except Exception as e:
            print(f"[应急监测][HTTP] 拉取应急行政区图层失败：{e}", flush=True)
    return {"response_board": board_res, "regions": regions_res}


def _extract_emergency_monitor_status_line(observation) -> str:
    data = _unwrap_tool_result(observation)
    data = _unwrap_scenario_http_payload(data)
    if isinstance(data, dict):
        for k in ("status_display", "status", "state_display", "state", "progress"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        meta = data.get("meta")
        if isinstance(meta, dict):
            for k in ("status_display", "status", "state"):
                v = meta.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return "状态已更新"


async def _run_emergency_monitor_loop(tools, user_text: str, max_rounds_override: int | None = None):
    interval = max(10, int(os.getenv("EMERGENCY_MONITOR_POLL_INTERVAL_SEC", "60")))
    max_rounds_env = int(os.getenv("EMERGENCY_MONITOR_MAX_ROUNDS", "0"))
    max_rounds = max_rounds_env
    if isinstance(max_rounds_override, int) and max_rounds_override > 0:
        max_rounds = max_rounds_override
    mode = os.getenv("EMERGENCY_MONITOR_MODE", "http_or_tool").strip().lower()
    tool = _find_preferred_emergency_monitor_tool(tools)
    tool_args = _load_emergency_monitor_tool_args()
    round_idx = 0
    while True:
        round_idx += 1
        try:
            used_http = False
            if mode in {"http", "http_or_tool"}:
                try:
                    monitor_http_res = await _run_emergency_monitor_http_round(user_text, round_idx)
                    board_res = monitor_http_res.get("response_board") if isinstance(monitor_http_res, dict) else None
                    regions_res = monitor_http_res.get("regions") if isinstance(monitor_http_res, dict) else None
                    linkage_scene_hint = os.getenv(
                        "EMERGENCY_MONITOR_LINKAGE_SCENE_HINT",
                        "应急响应 行政区 监测",
                    ).strip() or "应急响应 行政区 监测"
                    if regions_res:
                        await _send_gis_linkage(
                            tool_name="emergency_monitor_regions_http",
                            tool_args=_build_emergency_regions_payload_from_trigger(),
                            observation=regions_res,
                            user_text=linkage_scene_hint,
                            tools=tools,
                        )
                    await _send_gis_linkage(
                        tool_name="emergency_monitor_http_poll",
                        tool_args={},
                        observation=board_res,
                        user_text=linkage_scene_hint,
                        tools=tools,
                    )
                    used_http = True
                except Exception as http_err:
                    if mode == "http":
                        raise
                    print(f"[应急监测] HTTP 轮询失败，回退工具模式：{http_err}")

            if not used_http:
                if tool is None:
                    await cl.Message(
                        content="应急监测轮询未找到可用监测工具，已停止本次轮询。",
                        author="应急监测",
                    ).send()
                    return
                observation = await tool.ainvoke(tool_args)
                await _send_gis_linkage(
                    tool_name=getattr(tool, "name", "emergency_monitor"),
                    tool_args=tool_args,
                    observation=observation,
                    user_text=user_text,
                    tools=tools,
                )
                status_line = _extract_emergency_monitor_status_line(observation)
                if _is_emergency_monitor_verbose_updates_enabled():
                    await cl.Message(
                        content=f"第 {round_idx} 次应急监测已更新：{status_line}",
                        author="应急监测",
                    ).send()
        except asyncio.CancelledError:
            raise
        except Exception:
            await cl.Message(
                content="应急监测暂时失败，请稍后重试；如持续失败，请联系值班技术支持。",
                author="应急监测",
            ).send()

        if max_rounds > 0 and round_idx >= max_rounds:
            if _is_emergency_monitor_verbose_updates_enabled():
                await cl.Message(content="本次应急监测已完成。", author="应急监测").send()
            else:
                # 默认一次性查询时给业务侧一个明确收口，避免误以为仍在后台跑
                if max_rounds == 1:
                    await cl.Message(
                        content="本次应急响应查询已完成。如需持续监测，请说“持续监测应急响应”。",
                        author="应急监测",
                    ).send()
            return
        await asyncio.sleep(interval)


def _start_emergency_monitor_task(tools, user_text: str, max_rounds_override: int | None = None):
    prev_task = cl.user_session.get("emergency_monitor_task")
    if prev_task and hasattr(prev_task, "done") and not prev_task.done():
        prev_task.cancel()
    task = asyncio.create_task(_run_emergency_monitor_loop(tools, user_text, max_rounds_override=max_rounds_override))
    cl.user_session.set("emergency_monitor_task", task)
    return task


@cl.on_window_message
async def on_window_message_from_parent(message):
    """
    接收外层 GIS 页面通过 postMessage → custom_js(test.js) → socket.emit("window_message") 传来的数据。
    message 可能是 str（JSON 字符串）或已被反序列化的 dict（取决于客户端 emit 形态）。
    """
    payload = message
    if isinstance(message, str):
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            payload = {"raw": message}

    # test.js 对对象会包一层 { source, origin, data }；业务侧通常只关心 data
    if isinstance(payload, dict) and payload.get("source") == "parent_postmessage" and "data" in payload:
        cl.user_session.set("last_parent_postmessage_envelope", payload)
        normalized = payload["data"]
    else:
        normalized = payload

    # 部分环境下同一帧会触发两次 on_window_message（浏览器仅 emit 一次）；短时按内容去重
    try:
        dedupe_key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        dedupe_key = str(normalized)
    prev_key = cl.user_session.get("_gis_wm_dedupe_key")
    prev_t = cl.user_session.get("_gis_wm_dedupe_mono")
    now = time.monotonic()
    if prev_key == dedupe_key and isinstance(prev_t, (int, float)) and (now - prev_t) < 0.35:
        return
    cl.user_session.set("_gis_wm_dedupe_key", dedupe_key)
    cl.user_session.set("_gis_wm_dedupe_mono", now)

    cl.user_session.set("last_parent_postmessage", normalized)
    print(f"[GIS parent postMessage] {normalized!r}", flush=True)

    if isinstance(normalized, dict) and _is_gis_timeline_alert_payload(normalized):
        cl.user_session.set("last_gis_timeline_alert", normalized)
        try:
            text = _format_gis_alert_assistant_template(normalized)
            await cl.Message(content=text, author="预警推送").send()
        except Exception as e:
            print(f"[GIS alert] 推送会话消息失败（可能尚无活跃会话上下文）：{e!r}", flush=True)


# ===============================
# 2. 接收用户消息并流式回复
# ===============================
@cl.on_message
async def on_message(message: cl.Message):
    planner_chain = cl.user_session.get("planner_chain")
    answer_chain = cl.user_session.get("answer_chain")
    messages = cl.user_session.get("messages")
    tools = cl.user_session.get("tools")

    # 兜底：恢复线程或服务热重载后，若运行时对象缺失则即时重建，避免“可看历史但无法继续聊”。
    if planner_chain is None or answer_chain is None or tools is None:
        await _init_runtime_session(messages_seed=messages if isinstance(messages, list) else [])
        planner_chain = cl.user_session.get("planner_chain")
        answer_chain = cl.user_session.get("answer_chain")
        tools = cl.user_session.get("tools")
        messages = cl.user_session.get("messages")

    if not isinstance(messages, list):
        messages = []
        cl.user_session.set("messages", messages)

    if _is_help_command(message.content):
        text = _help_menu_text()
        await cl.Message(content=text, author="应急助手").send()
        messages.append(HumanMessage(content=message.content))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return

    if _is_stop_monitor_command(message.content):
        prev_task = cl.user_session.get("emergency_monitor_task")
        if prev_task and hasattr(prev_task, "done") and not prev_task.done():
            prev_task.cancel()
        cl.user_session.set("emergency_monitor_task", None)
        text = "已停止本次持续监测。需要时可再说“持续监测应急响应”。"
        await cl.Message(content=text, author="应急监测").send()
        messages.append(HumanMessage(content=message.content))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return

    if _is_emergency_monitor_command(message.content):
        continuous = _wants_continuous_monitor(message.content)
        # 默认一次性查询（查完就停）；只有明确说持续/轮询才常驻后台任务
        _start_emergency_monitor_task(tools, message.content, max_rounds_override=(0 if continuous else 1))
        text = "正在进行应急响应监测。" if continuous else "正在查询应急响应信息（本次查询完成后将自动结束）。"
        await cl.Message(content=text, author="应急监测").send()
        messages.append(HumanMessage(content=message.content))
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return

    # 发布确认快捷问答：发布确认 22 已发布 备注xxx
    if await _handle_publish_ack_command(message.content):
        messages.append(HumanMessage(content=message.content))
        messages.append(AIMessage(content="发布确认已处理"))
        cl.user_session.set("messages", messages)
        return

    # GIS 快捷问答：栅格列表 / 246分区河系 + times
    if await _handle_gis_quick_command(message.content, tools=tools):
        messages.append(HumanMessage(content=message.content))
        messages.append(AIMessage(content="GIS问答已处理"))
        cl.user_session.set("messages", messages)
        return

    callbacks = {
        "need_river_plot": _need_river_plot,
        "extract_river_name": _extract_river_name,
        "build_admin_overlay_for_plot": _build_admin_overlay_for_plot,
        "render_and_send_plot": render_and_send_plot,
        "build_river_network_brief": _build_river_network_brief,
        "append_followup_if_needed": _append_followup_if_needed,
        "stream_text_to_message": stream_text_to_message,
        "user_forbids_followup": _user_forbids_followup,
        "make_followup_question": _make_followup_question,
        "ainvoke_chain": ainvoke_chain,
        "should_force_admin_units_reply": _should_force_admin_units_reply,
        "should_force_partition_table_reply": _should_force_partition_table_reply,
        "should_force_structured_impact_reply": _should_force_structured_impact_reply,
        "build_admin_units_only_reply": _build_admin_units_only_reply,
        "build_partition_only_reply": _build_partition_only_reply,
        "build_structured_impact_reply": _build_structured_impact_reply,
        "enrich_with_impact_time_tool": _enrich_with_impact_time_tool,
        "tool_observation_to_text": _tool_observation_to_text,
        "guess_gis_scene": _guess_gis_scene,
        "send_gis_linkage": _send_gis_linkage,
    }

    await process_message(
        message=message,
        planner_chain=planner_chain,
        answer_chain=answer_chain,
        tools=tools,
        messages=messages,
        callbacks=callbacks,
    )
