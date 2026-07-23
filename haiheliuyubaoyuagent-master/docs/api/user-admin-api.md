# 用户管理后台接口说明（前端接入版）

> 本文档面向前端开发同事，说明项目中与用户/管理员相关的后台 REST 接口。

## 1. 概述

项目目前有两处提供用户管理接口：

| 服务 | 文件位置 | 说明 |
|------|----------|------|
| Chainlit 前端服务 | `chainlitexam/chain_gzt.py` | 与 Chainlit 聊天前端同进程运行，供管理后台页面直接调用 |
| MCP 工具服务 | `haihe-weather-analyzer-mcp/rest_api.py` | 独立 REST 服务，提供登录、注册、用户管理等基础认证能力 |

两套接口操作的是同一张 PostgreSQL 用户表 `hh_user_account`，角色体系一致。

## 2. 基础信息

### 2.1 Base URL

- **Chainlit 服务**：`http(s)://<chainlit-host>:<chainlit-port>`
  - 生产/测试环境以实际部署的 Chainlit 服务地址为准。
  - 本地开发统一通过 `chainlit run chain_gzt.py` 启动（**不要用 `uvicorn chain_gzt:app`**——
    虽然 `app.mount("/api/v1", api_sub_app)` 也会注册 `/api/v1/*`，但本地 `app`
    没有 Chainlit 的 chat/socket/登录页，前端不可用，只能当作 REST API 冒烟入口）。
  - 默认端口 `8000`，可通过 `--port` 覆盖。
- **MCP 服务**：`http(s)://<mcp-host>:<mcp-port>`
  - 以 `haihe-weather-analyzer-mcp/rest_api.py` 实际运行的地址为准。

### 2.2 通用请求格式

- Content-Type：`application/json`
- 字符编码：UTF-8

### 2.3 通用响应格式

```json
{
  "code": 200,
  "data": { ... },
  "message": "success"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 业务状态码，`200` 表示成功，非 `200` 表示失败 |
| `data` | any | 接口返回数据 |
| `message` | string | 提示信息 |

## 3. 认证方式

### 3.1 Chainlit 服务

Chainlit 服务复用 Chainlit 自身的登录态：

- 管理员必须先通过 Chainlit 登录页登录。
- 登录后，Chainlit 会在前端维护 session。
- 调用下文的管理接口时，请求需携带同域 cookie/session，后端通过 `cl.user_session.get("user")` 判断当前登录用户是否为 `admin`。
- 若未登录或当前用户非管理员，接口返回 `401/403`。

### 3.2 MCP 服务

MCP 服务采用 **HTTP Basic Auth**：

- 调用管理类接口时，需要在请求头中携带管理员账号密码：
  ```
  Authorization: Basic <base64(username:password)>
  ```
- 普通登录/注册接口无需 Basic Auth。

## 4. 接口列表

### 4.1 Chainlit 服务接口

文件：`chainlitexam/chain_gzt.py`

#### 4.1.1 用户注册

- **Method**：`POST`
- **Path**：`/api/v1/auth/register`
- **说明**：普通用户注册，**不允许注册管理员账号**。
- **请求体**：
  ```json
  {
    "username": "zhangsan",
    "password": "123456",
    "role": "external"
  }
  ```
- **字段说明**：
  | 字段 | 类型 | 必填 | 说明 |
  |------|------|------|------|
  | `username` | string | 是 | 用户名，1~64 字符 |
  | `password` | string | 是 | 密码 |
  | `role` | string | 否 | `admin / forecaster / external`，默认 `external`；此处传 `admin` 会报错 |
- **成功响应**：
  ```json
  {
    "code": 200,
    "data": {
      "username": "zhangsan",
      "role": "external",
      "role_label": "外部用户",
      "status": "active"
    },
    "message": "success"
  }
  ```

#### 4.1.2 获取用户列表

- **Method**：`GET`
- **Path**：`/api/v1/admin/users`
- **权限**：仅管理员
- **成功响应**：
  ```json
  {
    "code": 200,
    "data": [
      {
        "username": "admin",
        "role": "admin",
        "role_label": "管理员",
        "status": "active",
        "created_at": "2026-07-01 10:00:00",
        "updated_at": "2026-07-05 12:00:00"
      }
    ],
    "message": "success"
  }
  ```

#### 4.1.3 创建/覆盖用户

- **Method**：`POST`
- **Path**：`/api/v1/admin/users`
- **权限**：仅管理员
- **请求体**：同 `4.1.1`，但 `role` 可以传 `admin`。
- **说明**：
  - 若用户名已存在，会覆盖原密码、角色、状态为 `active`。
  - 可用于“创建用户”或“强制修改用户信息”。

#### 4.1.4 修改用户状态

- **Method**：`PATCH`
- **Path**：`/api/v1/admin/users/{username}/status`
- **权限**：仅管理员
- **请求体**：
  ```json
  {
    "status": "disabled"
  }
  ```
- **字段说明**：
  | 字段 | 类型 | 必填 | 说明 |
  |------|------|------|------|
  | `status` | string | 是 | `active` 或 `disabled` |
- **约束**：默认管理员账号（`admin`）不能被禁用。
- **成功响应**：
  ```json
  {
    "code": 200,
    "data": {
      "username": "zhangsan",
      "status": "disabled"
    },
    "message": "success"
  }
  ```

#### 4.1.5 重置用户密码

- **Method**：`POST`
- **Path**：`/api/v1/admin/users/{username}/reset-password`
- **权限**：仅管理员
- **请求体**：
  ```json
  {
    "password": "newpassword"
  }
  ```
- **说明**：重置密码后，用户状态会自动恢复为 `active`。

---

### 4.2 MCP 服务接口

文件：`haihe-weather-analyzer-mcp/rest_api.py`

#### 4.2.1 用户登录

- **Method**：`POST`
- **Path**：`/api/v1/auth/login`
- **请求体**：
  ```json
  {
    "username": "admin",
    "password": "admin123"
  }
  ```
- **成功响应**：
  ```json
  {
    "code": 200,
    "data": {
      "username": "admin",
      "role": "admin",
      "role_label": "管理员",
      "status": "active"
    },
    "message": "success"
  }
  ```
- **说明**：该接口仅做账号密码校验，不返回 Token。前端若对接 MCP 服务，管理接口需使用 Basic Auth。

#### 4.2.2 用户注册

- **Method**：`POST`
- **Path**：`/api/v1/auth/register`
- **请求体**：
  ```json
  {
    "username": "zhangsan",
    "password": "123456",
    "role": "external"
  }
  ```
- **说明**：普通注册不允许创建 `admin` 角色。

#### 4.2.3 获取用户列表

- **Method**：`GET`
- **Path**：`/api/v1/admin/users`
- **权限**：管理员 Basic Auth
- **成功响应**：同 Chainlit 服务，按 `created_at` 倒序排列。

#### 4.2.4 创建/覆盖用户

- **Method**：`POST`
- **Path**：`/api/v1/admin/users`
- **权限**：管理员 Basic Auth
- **请求体**：
  ```json
  {
    "username": "zhangsan",
    "password": "123456",
    "role": "forecaster"
  }
  ```
- **说明**：管理员可创建 `admin` 角色用户；用户名已存在时会覆盖。

#### 4.2.5 修改用户状态

- **Method**：`PATCH`
- **Path**：`/api/v1/admin/users/{username}/status`
- **权限**：管理员 Basic Auth
- **请求体**：
  ```json
  {
    "status": "disabled"
  }
  ```
- **约束**：默认管理员账号不能被禁用。

#### 4.2.6 重置用户密码

- **Method**：`POST`
- **Path**：`/api/v1/admin/users/{username}/reset-password`
- **权限**：管理员 Basic Auth
- **请求体**：
  ```json
  {
    "password": "newpassword"
  }
  ```

## 5. 数据字典

### 5.1 角色（role）

| 角色值 | 中文标签 | 说明 |
|--------|----------|------|
| `admin` | 管理员 | 拥有用户管理权限 |
| `forecaster` | 预报员 | 业务用户 |
| `external` | 外部用户 | 普通外部用户 |

### 5.2 状态（status）

| 状态值 | 说明 |
|--------|------|
| `active` | 启用，可正常登录 |
| `disabled` | 禁用，无法登录 |

## 6. 错误码

| HTTP 状态 | 含义 | 常见场景 |
|-----------|------|----------|
| `400` | 请求参数错误 | 角色/状态非法、注册时尝试创建 admin 等 |
| `401` | 未登录/未认证 | Chainlit 服务未登录；MCP 服务未携带 Basic Auth |
| `403` | 无权限 | 当前用户非管理员 |
| `404` | 用户不存在 | 重置密码、修改状态时目标用户不存在 |

响应示例：

```json
{
  "code": 403,
  "data": null,
  "message": "仅管理员可操作"
}
```

## 7. 前端接入注意事项

1. **CORS**
   - Chainlit 服务的跨域配置在 `chainlitexam/.chainlit/config.toml` 的 `allow_origins` 中。
   - MCP 服务在 `rest_api.py` 中设置了 `allow_origins=["*"]`。
   - 若管理后台部署在新域名/端口，请将前端地址加入 Chainlit 配置的 `allow_origins`。

2. **默认管理员账号**
   - 可通过环境变量配置：
     - `CHAINLIT_ADMIN_USERNAME`（默认 `admin`）
     - `CHAINLIT_ADMIN_PASSWORD`（默认 `admin123`）
   - 生产环境务必修改默认密码。

3. **Chainlit 服务与 MCP 服务的选择**
   - 如果管理后台与 Chainlit 聊天前端同域部署，**推荐直接调用 Chainlit 服务接口**，可复用登录态。
   - 如果管理后台需要独立对接 MCP 服务，则使用 Basic Auth 调用 MCP 接口。

4. **密码安全**
   - 前端传输密码时应使用 HTTPS。
   - 服务端会对密码做 `SHA-256` 哈希存储，前端无需也不能处理哈希。

5. **接口索引**
   - MCP 服务提供了 `GET /api/v1/endpoints` 接口，可动态获取所有可用接口列表，便于调试。

---

如有接口字段或权限逻辑变更，请同步更新本文档。
