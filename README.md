# Forge Agent

Forge Agent 是一个本地自主编程智能体：给它一个任务，它会读取代码、调用工具、修改文件、运行验证，并把过程记录到日志中。

---

## 亮点

- **自主执行任务**：从理解需求、探索代码到修改和验证，自动完成多步骤软件工程任务。
- **多模型支持**：可通过配置或命令切换 Claude、DeepSeek、OpenAI、Groq、Ollama。
- **安全工具调用**：内置风险分级、危险命令拦截和 HITL 人工确认。
- **上下文管理**：支持对话历史压缩、Prompt Cache、结构化上下文和长期记忆。
- **测试与防漂移机制**：测试失败会触发反思；pytest 路径不存在、未收集测试等场景会停止并明确报告。
- **工具耗时统计**：记录每个工具的调用次数、失败次数和耗时，方便定位慢操作和无效探索。
- **多模式协作**：支持 ReAct、Plan-and-Execute、Multi-Agent、GitHub Issue 自动修复等工作流。

---

## 快速启动

### 1. 安装

```bash
git clone <repo-url>
cd forge-agent
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\activate
```

macOS / Linux：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
pip install -e ".[dev]"
```

### 2. 配置模型

复制环境变量模板并填写 API Key：

```bash
cp .env.template .env
```

### 3. 启动交互模式

```bash
python -m entry.cli chat --repo .
```

```text
解释一下这个项目的核心模块
```

### 4. 运行一次性任务

```bash
python -m entry.cli run --repo . --task "反复读取 README.md 文件直到你找到一个不存在的章节叫 'Secret Section'" --max-steps 15
```

---

## 常用命令

### Chat 模式

```bash
python -m entry.cli chat --repo .
python -m entry.cli chat --repo . --model gpt-4o --provider openai
python -m entry.cli chat --repo . --sandbox
```

交互内常用命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/mode react\|plan\|auto\|multi-agent` | 切换工作模式 |
| `/model <name>` | 切换模型 |
| `/stats` | 查看会话统计 |
| `/compact` | 压缩上下文 |
| `/clear` | 清空当前会话历史 |
| `/exit` | 退出 |

### Run 模式

```bash
python -m entry.cli run --repo . --task "修复这个报错并验证"
python -m entry.cli run --repo . --task-file task.txt
python -m entry.cli run --repo . --task "..." --confirm
python -m entry.cli run --repo . --task "..." --sandbox
```

### 查看日志

```bash
python -m entry.cli log list
python -m entry.cli log show logs/<log-file>.jsonl
```

### GitHub Issue 自动修复

```bash
python -m entry.github_issue \
  --repo owner/repo \
  --issue 42 \
  --local-path /path/to/local/repo
```

---

## 本地测试

### Plan Mode 回归测试

项目根目录下的 `test_plan_mode.py` 包含 Plan-and-Execute 模式的回归测试用例，使用 `MockBackend` 模拟 LLM 响应：

```bash
pytest test_plan_mode.py -v
```
