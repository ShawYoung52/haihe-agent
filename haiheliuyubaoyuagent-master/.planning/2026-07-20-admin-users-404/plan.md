# 修复 `GET /api/v1/admin/users` 404

## 现象

前端同事访问 `http://127.0.0.1:60682/api/v1/admin/users` 返回 `404 Not Found`。
接口在 `docs/api/user-admin-api.md` §4.1.2 中描述，对应 `chainlitexam/chain_gzt.py:385`。

## 根因（已验证）

1. **Chainlit SPA 兜底路由吞掉了自定义路由**
   - `chainlit.server.app` 在 `server.py:1852` 执行 `app.include_router(router)`，把
     `@router.get("/{full_path:path}")`（`server.py:1840`，返回 index.html）注册到 app。
   - `chain_gzt.py:60-64` 之后才通过 `@_API_APP.get("/api/v1/admin/users")` 注册用户管理路由——
     注册顺序晚于兜底路由，Starlette 按注册顺序匹配，**兜底路由先命中，返回 200 HTML**。
   - 用 `fastapi.testclient.TestClient` 复现：`GET /api/v1/admin/users` → `200 text/html`（SPA 首页），
     而不是自定义路由的 401。

2. **如果用 `uvicorn chain_gzt:app` 启动，路由根本没注册在被服务的 app 上**
   - `chain_gzt.py:54` 定义了本地 `app = FastAPI(...)`。
   - `chain_gzt.py:60-64` 因为 `from chainlit.server import app as chainlit_app` 成功，
     `_API_APP` 被赋值为 `chainlit_app`，所有 `@_API_APP.*` 装饰器把路由注册到 chainlit_app。
   - 此时若用 `uvicorn chain_gzt:app` 启动，被服务的是本地 `app`，但它上面没有任何 `/api/v1/*` 路由——
     **直接 404**。端口号 `60682` 不是 `chainlit run` 的默认端口 `8000`，高度怀疑就是这种情况。

3. **次要 bug：`_require_admin_current_user` 在自定义 HTTP 路由里根本拿不到用户**
   - `chain_gzt.py:285-291` 使用 `cl.user_session.get("user")`。
   - `cl.user_session` 依赖 `context_var`（`chainlit/context.py:58`），只有 `init_ws_context` /
     `init_http_context` 会设置它。Chainlit 不会为自定义 HTTP 路由自动初始化 context。
   - 即使路由能匹配到，`cl.user_session.get("user")` 也会抛 `ChainlitContextException` → 500。
   - 正确做法是直接读 cookie 里的 JWT：`chainlit.auth.get_token_from_cookies(request.cookies)`
     + `decode_jwt(token)`（`chainlit/auth/__init__.py:58-83`）。

## 方案

把所有 `/api/v1/*` 路由搬到一个独立的 FastAPI 子应用 `api_sub_app` 上，路径去掉 `/api/v1` 前缀；
然后在两个入口都把它挂到 `/api/v1`：

- 本地 `app`（`uvicorn chain_gzt:app` 入口）：`app.mount("/api/v1", api_sub_app)`。
- `chainlit.server.app`（`chainlit run` 入口）：
  `chainlit_app.router.routes.insert(0, Mount("/api/v1", api_sub_app))`——
  **插到 routes 列表头部，确保早于 SPA 兜底路由**。

同时把 `_require_admin_current_user` 改为接收 `Request`，从 cookie 解 JWT 拿用户，不再依赖
`cl.user_session`。

### 具体改动（`chainlitexam/chain_gzt.py`）

1. 新增 `api_sub_app = FastAPI(title="海河流域用户管理 REST API", version="1.0.0")`。
2. 5 个 `@_API_APP.{post,get,patch}("/api/v1/...")` 装饰器全部改成 `@api_sub_app.{post,get,patch}("/...")`
   （去掉 `/api/v1` 前缀）：
   - `/auth/register`
   - `/admin/users`
   - `/admin/users` (POST)
   - `/admin/users/{username}/status` (PATCH)
   - `/admin/users/{username}/reset-password` (POST)
3. `_require_admin_current_user()` 改签名为 `_require_admin_current_user(request: Request)`，实现改为：
   ```python
   from chainlit.auth import get_token_from_cookies, decode_jwt
   token = get_token_from_cookies(request.cookies)
   if not token:
       raise HTTPException(401, "未登录")
   try:
       user = decode_jwt(token)
   except Exception:
       raise HTTPException(401, "无效的认证 token")
   metadata = getattr(user, "metadata", {}) or {}
   if metadata.get("role") != "admin":
       raise HTTPException(403, "仅管理员可操作")
   ```
4. 5 个路由函数签名增加 `request: Request` 形参，调用处改为 `_require_admin_current_user(request)`。
   `register_user` 不需要 admin 校验，无需改动。
5. 在路由定义之后、`_init_chainlit_data_layer` 之前，挂载：
   ```python
   from starlette.routing import Mount
   app.mount("/api/v1", api_sub_app)
   try:
       from chainlit.server import app as chainlit_app
       chainlit_app.router.routes.insert(0, Mount("/api/v1", api_sub_app))
   except Exception:
       pass
   ```
6. 删除 `chain_gzt.py:59-64` 的 `_API_APP` 三元赋值（不再需要）。

### 验证

- 启动 Chainlit：`chainlit run chain_gzt.py --port 8000`，未登录 `curl /api/v1/admin/users` → `401`。
- 启动 uvicorn：`uvicorn chain_gzt:app --port 8000`，未登录 `curl /api/v1/admin/users` → `401`。
- 用管理员登录 Chainlit 后，浏览器带 cookie 调 `GET /api/v1/admin/users` → `200` 返回用户列表。
- 用非管理员登录后调用 → `403`。

## 风险

- `chainlit_app.router.routes.insert(0, ...)` 是直接操作内部列表，依赖 Starlette 路由匹配按顺序。
  已通过 TestClient 验证可行。如果未来 Chainlit 改成 `Mount` 优先级或重新排路由，需要重新验证。
- `app.mount("/api/v1", api_sub_app)` 会让 `uvicorn chain_gzt:app` 也能服务用户管理接口，
  但本地 `app` 上原本就没有别的路由——这是预期的。
- `decode_jwt` 在 `chainlit.auth` 中已导出，无需新依赖。
