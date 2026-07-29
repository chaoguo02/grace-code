# Grace Code

*上次更新：2026-07-29 14:07*

Claude Code 架构对齐的自主编程智能体框架。支持 ReAct 循环、流式工具执行、子代理编排、MCP 协议、权限管线、上下文压缩和持久记忆。

---

## 快速启动

### 1. 安装

```bash
git clone <repo-url>
cd grace-code
python -m venv .venv
```

Windows:
```powershell
.venv\Scripts\activate
```

macOS / Linux:
```bash
source .venv/bin/activate
```

安装依赖:
```bash
pip install -e ".[dev]"
```

### 2. 配置模型

复制环境变量模板并填写 API Key：

```bash
cp .env.template .env
```

支持的 provider: `deepseek`（默认）、`openai`、`anthropic`、`groq`、`ollama`。

### 3. 一次性任务（Run 模式）

```bash
# 基础用法
python -m entry.cli run --repo . --task "分析项目结构并列出所有 Python 文件"

# 指定模型
python -m entry.cli run --repo . --task "修复这个报错并验证" --model deepseek-chat

# Plan → Execute 流程
python -m entry.cli run --repo . --agent plan --intent analysis --task "设计一个日志模块"
```

### 4. 交互式对话（Chat 模式）

```bash
python -m entry.cli chat --repo .
```

交互内命令:

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/mode react\|plan\|auto` | 切换工作模式 |
| `/model <name>` | 切换模型 |
| `/stats` | 查看会话统计 |
| `/compact` | 压缩上下文 |
| `/clear` | 清空当前会话历史 |
| `/exit` | 退出 |

### 5. Web 界面（npm 启动）

项目包含一个 Vite + React 构建的 Web 前端，通过 npm 管理。

#### 安装 Node.js 依赖

```bash
# 安装 Web 前端依赖
cd web
npm install
```

#### 开发模式（热重载）

需要同时启动后端和前端两个进程：

```bash
# 终端 1：启动 Python 后端 API 服务（默认端口 8765）
python -m server.main --repo .

# 终端 2：启动 Vite 开发服务器（HMR 热重载）
cd web && npm run dev
```

前端开发服务器默认运行在 `http://localhost:5173`，API 请求会自动代理到后端。

#### 生产模式

构建前端产物并启动后端服务（由后端统一提供静态文件服务）：

```bash
# 构建前端 + 启动后端（一行命令）
npm start
```

或者分步执行：

```bash
# 先构建前端
npm run build:web

# 再启动后端（前端产物由后端 / 路由提供）
npm run server
```

访问 `http://localhost:8765` 即可使用 Web 界面。

---

## 架构

```
entry/cli.py → AgentConfig → ReActAgent.run()
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
             StreamingTool    Permission    Compaction
             Executor         Pipeline      Pipeline
                    │             │             │
                    ▼             ▼             ▼
              LLM Backend     ToolRegistry   MemoryStore
              (DeepSeek /     (BaseTool +    (MEMORY.md +
               OpenAI /        Hook +         metadata +
               Anthropic)      Policy)        DreamAgent)
```

### 六大子系统

| 子系统 | 能力 |
|--------|------|
| **ReAct** | 流式 dispatch、per-call 并发安全、5 层压缩管道（Budget→Snip→Micro→Collapse→AutoCompact）、4 种恢复路径、immutable AgentTurnState |
| **Plan** | Session 连续性、prompt-based permissions、JSON contract、prompt 节流 |
| **Subagent** | Fork/Worktree、background 默认、live steering、nested delegation、_ChildTurnPhase 生命周期 |
| **MCP** | 4 种 transport（stdio/HTTP/SSE/WS）、agent-scoped 生命周期、自动重连、ToolSearch |
| **Skills** | 文件系统 Skill、runtime-based 安全执行、SkillContextModifier、$ARGUMENTS 替换 |
| **Hooks** | 10 种 HookEvent、per-session 隔离、PreToolUse updatedInput、PostToolUse updatedToolOutput、PostResponse |

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `FORGE_STREAMING` | `1` | 流式 dispatch（设为 `0` 禁用） |
| `FORGE_NUDGE` | `1` | Token 预算续接 nudge（设为 `0` 禁用） |
| `STATE_HOME` | `~/.grace-code/state` | 会话和记忆持久化路径 |

---

## 测试

```bash
# 回归测试（MockBackend）
pytest tests/test_cc_alignment_features.py tests/test_plan_approval.py -q

# CC 对齐检查
python tests/manual/verify_cc_alignment.py

# 端到端测试（需 DEEPSEEK_API_KEY）
pytest tests/test_smoke_e2e.py -v -m e2e
```
