# 用户体系接口文档

## 1. 角色约定

系统当前只允许以下 3 个角色：

- `admin`：管理员
- `forecaster`：预报员
- `external`：外部用户

### 角色中文名

| role | role_label |
|---|---|
| admin | 管理员 |
| forecaster | 预报员 |
| external | 外部用户 |

---

## 2. 用户表

表名：`public.hh_user_account`

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BIGSERIAL | 主键 |
| username | VARCHAR(64) | 用户名，唯一 |
| password_hash | VARCHAR(128) | 密码哈希 |
| role | VARCHAR(32) | 角色 |
| status | VARCHAR(16) | 状态：`active` / `disabled` |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

---

## 3. 通用返回格式

所有用户相关接口统一返回：

```json
{
  "code": 200,
  "data": {},
  "message": "success"
}
```

失败时通常返回：

```json
{
  "detail": "错误原因"
}
```

---

## 4. 登录与注册

### 4.1 注册用户

`POST /api/v1/auth/register`

#### 说明
注册普通用户。不能注册管理员账号。

#### 请求体

```json
{
  "username": "user1",
  "password": "123456",
  "role": "external"
}
```

#### 请求字段

| 字段 | 必填 | 说明 |
|---|---|---|
| username | 是 | 用户名，1~64 字符 |
| password | 是 | 密码 |
| role | 否 | 默认 `external`，只能是 `admin / forecaster / external` |

#### 约束

- `role = admin` 时会返回 400
- 同名用户已存在时，会覆盖密码和角色，并恢复为 `active`

#### 响应示例

```json
{
  "code": 200,
  "data": {
    "username": "user1",
    "status": "active",
    "role": "external",
    "role_label": "外部用户"
  },
  "message": "success"
}
```

---

### 4.2 Chainlit 登录

`@cl.password_auth_callback` 内部逻辑使用数据库校验账号密码。

#### 登录成功后返回的用户对象

- `identifier` = 用户名
- `display_name` = 角色中文名
- `metadata.role` = 角色值

示例：

```json
{
  "identifier": "admin",
  "display_name": "管理员",
  "metadata": {
    "role": "admin"
  }
}
```

---

## 5. 管理员用户管理接口

以下接口都要求当前登录用户角色为 `admin`。

### 5.1 获取用户列表

`GET /api/v1/admin/users`

#### 说明
获取全部用户，按创建时间和用户名排序。

#### 响应字段

| 字段 | 说明 |
|---|---|
| username | 用户名 |
| role | 角色值 |
| role_label | 角色中文名 |
| status | 状态 |
| created_at | 创建时间 |
| updated_at | 更新时间 |

#### 响应示例

```json
{
  "code": 200,
  "data": [
    {
      "username": "admin",
      "role": "admin",
      "role_label": "管理员",
      "status": "active",
      "created_at": "2026-06-22 10:00:00",
      "updated_at": "2026-06-22 10:00:00"
    }
  ],
  "message": "success"
}
```

---

### 5.2 管理员创建用户

`POST /api/v1/admin/users`

#### 请求体

```json
{
  "username": "forecaster1",
  "password": "123456",
  "role": "forecaster"
}
```

#### 请求字段

| 字段 | 必填 | 说明 |
|---|---|---|
| username | 是 | 用户名 |
| password | 是 | 密码 |
| role | 否 | 默认 `external`，只能是 `admin / forecaster / external` |

#### 说明

- 创建成功后状态默认是 `active`
- 如果同名用户已存在，会覆盖密码和角色，并恢复为 `active`

#### 响应示例

```json
{
  "code": 200,
  "data": {
    "username": "forecaster1",
    "status": "active",
    "role": "forecaster",
    "role_label": "预报员"
  },
  "message": "success"
}
```

---

### 5.3 修改用户状态

`PATCH /api/v1/admin/users/{username}/status`

#### 路径参数

| 参数 | 说明 |
|---|---|
| username | 用户名 |

#### 请求体

```json
{
  "status": "disabled"
}
```

#### 请求字段

| 字段 | 必填 | 说明 |
|---|---|---|
| status | 是 | 只能是 `active` 或 `disabled` |

#### 约束

- 默认管理员 `admin` 不能被禁用
- 用户不存在时返回 404

#### 响应示例

```json
{
  "code": 200,
  "data": {
    "username": "forecaster1",
    "status": "disabled"
  },
  "message": "success"
}
```

---

### 5.4 重置密码

`POST /api/v1/admin/users/{username}/reset-password`

#### 路径参数

| 参数 | 说明 |
|---|---|
| username | 用户名 |

#### 请求体

```json
{
  "password": "newpass123"
}
```

#### 请求字段

| 字段 | 必填 | 说明 |
|---|---|---|
| password | 是 | 新密码 |

#### 说明

- 密码会被重新哈希写入数据库
- 状态会自动恢复为 `active`

#### 响应示例

```json
{
  "code": 200,
  "data": {
    "username": "forecaster1",
    "status": "active",
    "role": "forecaster",
    "role_label": "预报员"
  },
  "message": "success"
}
```

---

## 6. 默认管理员

系统启动时会自动种一个默认管理员：

- 用户名：`admin`
- 密码：`admin123`
- 角色：`admin`
- 状态：`active`

---

## 7. 前端对接建议

### 登录后权限判断

- `admin`：展示用户管理入口
- `forecaster`：展示预报员功能
- `external`：只展示普通访问能力

### 用户列表展示建议

显示以下字段即可：

- 用户名
- 角色中文名 `role_label`
- 状态
- 创建时间
- 更新时间

### 角色下拉建议

下拉值只保留：

- `管理员` → `admin`
- `预报员` → `forecaster`
- `外部用户` → `external`

---

## 8. 注意事项

- 管理员接口都依赖当前 Chainlit 登录态中的 `metadata.role = admin`
- 注册接口只允许创建普通用户或预报员，不允许创建管理员
- 当前用户体系使用的是单表 `hh_user_account`，没有拆分角色表