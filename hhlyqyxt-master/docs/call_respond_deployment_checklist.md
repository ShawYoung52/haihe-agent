# 叫应功能内网部署检查清单

> 牵引智能体"叫应功能"上线前逐项核对。目标环境：Linux 调度器（`station-process-min`）+ Windows DMZ 微信网关（`WechatRPA/gateway`）。

## 一、数据库（内网 PostgreSQL）

- [ ] 执行 `scripts/create_call_respond_tables.sql` 建表（幂等，可重复）
- [ ] 验证两张表存在：`qy_call_respond_task`、`qy_call_respond_send_log`
- [ ] 索引存在：`idx_qy_call_respond_task_status`、`idx_qy_call_respond_task_emergency_monitor_id`、`idx_qy_call_respond_send_log_task_id`

## 二、调度器侧（Linux，`station-process-min` 服务）

- [ ] 代码已更新：`git pull origin main`（含 `ScheduledTask/call_respond.py`、`stationProcessMin.py` 集成）
- [ ] 环境变量已设置：
  - `WECHAT_GATEWAY_URL` = 微信网关地址（如 `http://<DMZ服务器IP>:8000`）
  - `WECHAT_GATEWAY_TOKEN` = 网关 token（与网关 `WECHAT_GATEWAY_TOKEN` 一致）
  - 未设置时 `send_file` 返回 False → 任务 `failed`（可重试），不崩溃
- [ ] 重启服务：`systemctl restart station-process-min`
- [ ] 日志无报错：`journalctl -u station-process-min -f`

## 三、微信网关侧（Windows DMZ，WechatRPA/gateway）

- [ ] 微信已登录、桌面未锁屏、同一 Windows 用户运行
- [ ] 网关已启动：`POST /health` 返回 OK
- [ ] **IP 白名单**：调度器 Linux 服务器 IP 已加入 `gateway_server.py` 的 `ALLOWED_CLIENT_IPS`（否则 403）
- [ ] **群白名单**：乙方确认的正式群名已加入 `ALLOWED_TARGETS`（当前仅测试群）
- [ ] env `WECHAT_GATEWAY_TOKEN` 已设置（与调度器一致）
- [ ] 文件后缀在 `ALLOWED_FILE_EXTENSIONS`（.docx/.pdf 均支持）

## 四、群映射配置（乙方提供群名后）

- [ ] 编辑 `call_respond_config.json` 的 `groups` 字段：`{城市: [群名, ...]}`
  - 群名必须与网关 `ALLOWED_TARGETS` 一致（否则网关拒绝）
  - 未配置时任务挂起 `pending_send`，配置后自动补发
- [ ] 话术模板 `template` 可自定义（`{city}`/`{level}` 占位符）

## 五、验证清单

- [ ] 应急响应等级变化时，`qy_call_respond_task` 生成新任务（同等级不重复）
- [ ] 调 `POST /tool/call-respond/{id}/confirm` → 返回 `confirmed`，后台开始发送
- [ ] 微信群收到话术 + 报告文件（docx 下载 / pdf 查看）
- [ ] `qy_call_respond_send_log` 逐群记录 `success`
- [ ] 群未配置 → 任务 `pending_send` 不报错；配置后自动补发
- [ ] 报告缺失 → 任务 `suspended`；报告回填后自动补发
- [ ] 发送失败不阻塞 5 分钟调度器主流程

## 六、API 冒烟（FastAPI 7000 端口）

- [ ] `GET /tool/call-respond/tasks?status=pending` 返回待确认任务
- [ ] `POST /tool/call-respond/{id}/confirm`（body `{"confirm_person":"张三"}`）
- [ ] `GET /tool/call-respond/{id}/logs` 返回逐群日志
- [ ] `POST /tool/call-respond/{id}/retry` 手动补发（不存在 id 返回 404）