-- ============================================================
-- 叫应功能建表脚本（内网 PostgreSQL 执行）
-- 表：qy_call_respond_task（叫应任务）+ qy_call_respond_send_log（逐群发送日志）
-- 用法：psql -h <内网IP> -U postgres -d <db> -f create_call_respond_tables.sql
-- 幂等：IF NOT EXISTS，可重复执行
-- ============================================================

-- 1. 叫应任务表
CREATE TABLE IF NOT EXISTS qy_call_respond_task (
    id SERIAL PRIMARY KEY,
    emergency_monitor_id INTEGER,          -- 关联 qy_emergency_response_monitor.id
    response_level SMALLINT NOT NULL DEFAULT 0,  -- 1=Ⅰ级 2=Ⅱ级 3=Ⅲ级 4=Ⅳ级 0=无
    datatime TIMESTAMP,                    -- 本次应急响应结束时间
    impact_city VARCHAR(512),              -- 受影响城市（快照，来自 qy_minute_monitor.impact_city）
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending/confirmed/sending/sent/pending_send/suspended/failed
    report_docx_path VARCHAR(512),         -- 报告 docx URL（下载源，发送时按 emergency_monitor_id 反查）
    report_pdf_path VARCHAR(512),          -- 报告 pdf URL（同上）
    confirm_person VARCHAR(64),            -- 确认人（前端传入）
    confirm_time TIMESTAMP,                -- 确认时间
    send_time TIMESTAMP,                   -- 首次发送完成时间
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_status
    ON qy_call_respond_task(status);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_emergency_monitor_id
    ON qy_call_respond_task(emergency_monitor_id);

-- 2. 逐群发送日志表
CREATE TABLE IF NOT EXISTS qy_call_respond_send_log (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL,              -- 关联 qy_call_respond_task.id
    target_group VARCHAR(128),             -- 目标群名
    status VARCHAR(20) NOT NULL DEFAULT 'success',  -- success/failed/skipped
    detail VARCHAR(512),                   -- 失败原因等
    send_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_qy_call_respond_send_log_task_id
    ON qy_call_respond_send_log(task_id);

-- ============================================================
-- 迁移（若表已存在，仅新增字段）：
-- ALTER TABLE qy_call_respond_task ADD COLUMN IF NOT EXISTS report_docx_path VARCHAR(512);
-- ALTER TABLE qy_call_respond_task ADD COLUMN IF NOT EXISTS report_pdf_path VARCHAR(512);
-- ============================================================