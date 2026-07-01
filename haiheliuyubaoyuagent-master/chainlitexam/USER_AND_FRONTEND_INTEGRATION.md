# 用户体系与前端对接文档

## 1. 目标

本文档整理当前项目的用户体系接口、字段说明，以及前端页面的对接方式。

当前系统支持：

- 登录
- 注册普通用户
- 管理员用户管理
- 按角色展示不同功能入口

---

## 2. 角色约定

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

## 3. 数据表

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

## 4. 通用返回格式

用户相关接口统一返回：

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

## 5. 后端接口清单

### 5.1 注册用户

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

### 5.2 Chainlit 登录

`@cl.password_auth_callback` 内部使用数据库校验账号密码。

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

### 5.3 获取用户列表

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

### 5.4 管理员创建用户

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

### 5.5 修改用户状态

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

### 5.6 重置密码

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

## 7. 前端页面结构建议

### 7.1 登录页

**作用**

- 用户输入用户名和密码
- 登录成功后进入智能体主界面
- 根据角色显示不同功能入口

**交互流程**

1. 输入用户名、密码
2. 点击登录
3. 调用后端登录能力
4. 登录成功后读取用户角色
5. 按角色跳转或显示对应菜单

**登录后角色处理建议**

- `admin`：进入管理员视图
- `forecaster`：进入预报员视图
- `external`：进入普通用户视图

---

### 7.2 注册页

**作用**

- 普通用户注册
- 可选择角色，但不能选择管理员

**交互流程**

1. 输入用户名、密码
2. 选择角色
3. 提交注册
4. 注册成功后提示登录

**角色选择建议**

下拉只保留：

- 外部用户（`external`）
- 预报员（`forecaster`）

不允许展示管理员选项。

---

### 7.3 管理员用户管理页

**作用**

仅管理员可见，用于管理系统用户。

**建议模块**

- 用户列表
- 创建用户
- 修改状态
- 重置密码

**页面结构建议**

- 顶部：搜索框、刷新按钮、新建用户按钮
- 中间：用户表格
- 右侧或弹窗：新建用户 / 重置密码 / 修改状态表单

---

## 8. 前端对接流程

### 8.1 登录成功后

- `admin` → 智能体主界面 + 用户管理入口
- `forecaster` → 智能体主界面 + 预报员工作台
- `external` → 智能体主界面 + 普通用户主页

### 8.2 用户管理页进入后

- 先加载列表
- 可新增用户
- 可禁用/启用用户
- 可重置密码

### 8.3 表单字段建议

#### 登录表单

- username
- password

#### 注册表单

- username
- password
- role 下拉

#### 用户管理表单

- username
- password
- role
- status

---

## 9. 权限展示规则

### 9.1 `admin`

展示：

- 用户管理入口
- 所有普通功能
- 预报相关功能
- 智能体主界面全功能

### 9.2 `forecaster`

展示：

- 预报相关功能
- 普通功能

不展示：

- 用户管理入口

### 9.3 `external`

展示：

- 普通功能

不展示：

- 用户管理入口
- 预报员专属功能

---

## 10. 前端对接注意事项

1. 不要把管理员账号放到注册页里
2. 用户管理页只给管理员看
3. 用户状态变化后要刷新列表
4. 重置密码后建议提示“需要重新登录”
5. 前端保存登录态时要带上角色信息，方便路由鉴权
6. 注册接口只允许创建普通用户或预报员，不允许创建管理员

---

## 11. 推荐页面流转

### 登录成功后

- `admin` → 智能体主界面 + 用户管理入口
- `forecaster` → 智能体主界面 + 预报员工作台
- `external` → 智能体主界面 + 普通用户主页

### 管理员进入用户管理页后

- 先加载列表
- 可新增用户
- 可禁用/启用用户
- 可重置密码

---