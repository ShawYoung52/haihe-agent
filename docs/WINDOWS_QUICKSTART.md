# Windows 快速启动

本文适用于 Windows + Git Bash / PowerShell。

## 1. 克隆当前优化分支

如果你已经新建了空文件夹，并且当前就在该空文件夹里，可以执行：

```bash
git clone -b chore/web-cleanup-20260702 https://github.com/ShawYoung52/haihe-agent.git .
```

如果当前文件夹不是空的，建议在上一级目录执行：

```bash
git clone -b chore/web-cleanup-20260702 https://github.com/ShawYoung52/haihe-agent.git
cd haihe-agent
```

## 2. Git Bash 启动

```bash
bash scripts/run_mcp_backend.sh
bash scripts/run_chainlit_frontend.sh
```

## 3. PowerShell 启动

第一次可先初始化依赖：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_windows.ps1
```

启动后端：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_mcp_backend.ps1
```

另开一个 PowerShell 窗口启动前端：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_chainlit_frontend.ps1
```

## 4. 访问地址

```text
MCP 后端: http://127.0.0.1:3333/sse
Chainlit 前端: http://127.0.0.1:8003
```

## 5. 常见问题

### fatal: not a git repository

说明你当前目录不是 Git 仓库。空文件夹需要先执行 `git clone`。

### make 不可用

Windows 上可以直接用 Python 命令替代：

```bash
python scripts/check_repository.py
python -m unittest discover -s tests
```

### PowerShell 不允许执行脚本

使用下面形式启动：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_chainlit_frontend.ps1
```

## 6. 当前保留的工程目录

仓库当前只保留两个主工程：

```text
haiheliuyubaoyuagent-master/chainlitexam
haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp
```

旧目录如 `chainlitexam-gis`、`mcpexam`、`weather-analyzer-mcp-20260206` 已从分支移除，并已加入健康检查黑名单。
