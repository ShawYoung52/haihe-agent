# 快捷问题（按角色）接口对接文档

面向前端快捷问题面板：按「指定角色」查询该角色可见的快捷问题分区。

## 1. Base URL

- **Chainlit 服务**：`http(s)://<chainlit-host>:<chainlit-port>`
  - 与 `/api/v1/qa/ask`、`/api/v1/admin/users` 同一服务、同一 `api_sub_app`。
  - 生产/测试环境以实际部署的 Chainlit 服务地址为准。

## 2. 通用响应格式

```json
{
  "code": 200,
  "data": { ... },
  "message": "success"
}
```

## 3. 接口

### 3.1 查询指定角色的快捷问题

- **Method**：`GET`
- **Path**：`/api/v1/qa/quick-questions`
- **认证**：无（与 `/qa/ask` 同一网络层鉴权模型，靠部署时网络层限制；角色由调用方传入）

> **安全口径**：`role` 参数是**界面定制**，不是访问控制边界。快捷问题只是"提问建议"，
> 对应的真实数据问答走 `/qa/ask`、本就不按角色挡；即使调用方虚报 `role=admin`，也只是
> 多看到几个建议词条，不构成数据越权。真正敏感的网络隔离仍由部署层负责。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role` | string | 否 | `admin` / `forecaster` / `external`（大小写不敏感、自动去空格）。<br>**显式传入优先**；不传时后端回退读 Chainlit 登录 cookie 的 `metadata.role`；两者都没有按 `external`（公众，最严格）兜底。<br>显式传入非法值 → `400`。 |

#### 角色 → 可见分区

| 角色 | 可见分区 |
|------|----------|
| `admin` | 全部 8 个分区 |
| `forecaster` | 01 天气资讯、02 防汛服务（实况/预报/预警 + 河系水库降雨预报与风险，即区局预报员日常口径） |
| `external` | 仅 01 天气资讯 |

> 角色→分区映射维护在后端 `chainlitexam/quick_questions.py` 的 `_ROLE_SECTION_IDS`，
> 调整权限改这里即可，不用动前端静态 `quickQA.json`。
>
> **数据源**：接口读的是后端自带的 `chainlitexam/config/quickQA.json`（随 Chainlit 服务一起部署），
> **不是** `AgentWeb/config/quickQA.json`——AgentWeb 在服务器上是独立部署到 Tomcat 的前端静态包，
> 与 Chainlit 服务不在一起。前端面板改走本接口后，以后端这份为唯一数据源；
> AgentWeb 那份仅留给旧的静态 fetch 路径，两份内容当前一致。

#### 成功响应示例（`role=external`）

```json
{
  "code": 200,
  "data": {
    "role": "external",
    "sections": [
      {
        "id": 1,
        "type": "01 天气资讯",
        "sub": "(天气资讯...)",
        "iconKey": "liveBroadcast",
        "isOpen": true,
        "questions": ["今日雨情？", "天津当前天气实况？", "..."]
      }
    ]
  },
  "message": "success"
}
```

#### 字段说明

`data.sections[]` 与前端静态 `config/quickQA.json` 的分区结构完全一致：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 分区 id |
| `type` | string | 分区标题（如「01 天气资讯」） |
| `sub` | string | 分区副标题 |
| `iconKey` | string | 图标 key |
| `isOpen` | bool | 面板默认是否展开 |
| `questions` | string[] | 该分区的快捷问题列表 |

#### 错误响应

| code | 触发条件 |
|------|----------|
| `400` | 显式传入的 `role` 不是 `admin`/`forecaster`/`external` |

## 4. 前端用法

前端登录后已知当前用户角色（登录接口返回的 `role`），直接拼接查询：

```
GET /api/v1/qa/quick-questions?role=forecaster
```

- 面板内容改由本接口下发后，前端可不再静态 `fetch("./config/quickQA.json")`；
  两者数据源内容当前一致、结构字段不变，前端渲染逻辑无需改动。
- 未登录/匿名场景：不传 `role` 或传 `external`，只返回 01 天气资讯一个分区。
