-- Chainlit 最小补字段脚本（PostgreSQL）
-- 目的：在不破坏旧数据的前提下，补齐当前版本常用但库里缺失的字段。
-- 使用前请先备份数据库。

BEGIN;

-- 1) steps.defaultOpen 缺失会导致 on_chat_start / on_message 写入失败
ALTER TABLE IF EXISTS "steps"
  ADD COLUMN IF NOT EXISTS "defaultOpen" BOOLEAN NOT NULL DEFAULT FALSE;

-- 1.1) steps.autoCollapse 缺失会导致 step 创建/更新失败
ALTER TABLE IF EXISTS "steps"
  ADD COLUMN IF NOT EXISTS "autoCollapse" BOOLEAN NOT NULL DEFAULT FALSE;

-- 2) elements.props 缺失会导致读取线程元素时报错
ALTER TABLE IF EXISTS "elements"
  ADD COLUMN IF NOT EXISTS "props" JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;

