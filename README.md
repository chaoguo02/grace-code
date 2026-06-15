# Forge Agent

自主编程智能体。给它一个任务描述，它会自己探索代码库、修改文件、运行测试，直到完成。

支持 **Claude、DeepSeek、OpenAI、Groq、Ollama** 多种模型，内置流式输出、Docker 沙箱、GitHub Issue 自动修复。

---

## 快速开始

```bash
# 克隆 & 安装
git clone <repo-url> && cd forge-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 配置（复制模板，填入 API Key）
cp .env.template .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=sk-xxx

# 启动交互对话
python -m entry.cli chat --repo .
```

> `.env` 启动时自动加载（python-dotenv），无需手动 export。

---

## 配置

所有 LLM 配置通过 `.env` 文件管理，敏感信息不入库：

```bash
# .env（从 .env.template 复制）
FORGE_LLM_PROVIDER=deepseek                    # deepseek / openai / anthropic / groq / ollama
FORGE_LLM_MODEL=deepseek/deepseek-v4-flash     # 模型名称
FORGE_LLM_BASE_URL=https://api.llm.mioffice.cn/v1/  # API 地址
DEEPSEEK_API_KEY=sk-xxx                        # API Key
HF_ENDPOINT=https://hf-mirror.com             # embedding 模型下载镜像
```

配置优先级：**CLI 参数 > .env 环境变量 > config/default.yaml 默认值**

运行时可通过 `/model` 和 `/mode` 命令动态切换，无需重启。

---

## 使用方式

### chat 模式（推荐）

持续对话，每轮历史保留，最接近 Claude Code 的体验：

```bash
python -m entry.cli chat                          # 当前目录
python -m entry.cli chat --repo /path/to/project  # 指定目录
python -m entry.cli chat --model gpt-4o           # 临时切换模型
python -m entry.cli chat --sandbox                # Docker 沙箱
```

对话内命令：

| 命令 | 作用 |
|------|------|
| `/exit` | 退出 |
| `/mode react\|plan\|auto` | 切换 agent 模式 |
| `/model <name>` | 切换 LLM 模型 |
| `/compact` | 压缩对话历史 |
| `/stats` | 查看统计 |
| `/clear` | 清空历史 |
| `/help` | 帮助 |

### run 模式

一次性任务，适合明确的批处理场景：

```bash
python -m entry.cli run --task "修复所有 failing 的测试"
python -m entry.cli run --task-file task.txt
python -m entry.cli run --task "..." --confirm     # 危险命令需确认
python -m entry.cli run --task "..." --sandbox     # Docker 沙箱
```

### plan 模式

复杂任务先拆解为 DAG，再按依赖顺序执行：

```bash
# chat 内通过 /mode 切换
/mode plan
重构 api.py，拆分成更小的函数并补测试
```

### GitHub Issue 自动修复

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
    --repo owner/repo --issue 42 --local-path /tmp/myrepo
```

---

## 架构

```
forge-agent/
├── agent/              # 核心 Agent 引擎
│   ├── core.py         # ReAct 主循环（思考→行动→观察）
│   ├── plan.py         # Plan-and-Execute（DAG 调度）
│   ├── factory.py      # Agent 工厂（react/plan/auto 模式选择）
│   ├── task.py         # Task / Action / Observation 数据模型
│   ├── event_log.py    # JSONL append-only 事件流
│   └── prompt.py       # System prompt 模板
│
├── llm/                # LLM 后端抽象
│   ├── base.py         # LLMBackend 基类 + native tool_use
│   ├── anthropic_backend.py   # Claude（原生 tool_use + 流式）
│   ├── openai_backend.py      # OpenAI / DeepSeek / Groq / Ollama
│   └── router.py       # 按配置自动选择 backend
│
├── tools/              # 工具层（Agent 可调用的操作）
│   ├── file_tool.py    # 文件读写查看
│   ├── shell_tool.py   # Shell 执行（三层安全防护）
│   ├── search_tool.py  # 文本搜索 / 文件查找 / 符号定位
│   ├── git_tool.py     # git status / diff / add / commit
│   ├── memory_tool.py  # 记忆读写搜索（含 RAG 向量检索）
│   ├── web_tool.py     # web_search + web_fetch
│   ├── runtime.py      # LocalRuntime / DockerRuntime 沙箱
│   └── mcp_client.py   # MCP 外部工具服务器连接
│
├── memory/             # 三层记忆系统
│   ├── store.py        # 文件型长期记忆（YAML frontmatter .md）
│   ├── external_store.py # SQLite + 向量语义搜索
│   ├── chunker.py      # 语义分块（段落/标题 + 滑动窗口）
│   ├── indexer.py      # 写入时自动向量索引
│   ├── retriever.py    # 主动检索（每轮自动注入相关记忆）
│   ├── context.py      # 记忆上下文注入管理
│   └── proactive.py    # 主动记忆检测（用户偏好/命令模式）
│
├── context/            # 上下文管理
│   ├── repo_map.py     # tree-sitter 多语言符号提取
│   ├── history.py      # 对话历史滑动窗口
│   ├── token_budget.py # Token 预算管理
│   └── compaction.py   # 多层上下文压缩
│
├── entry/              # 入口层
│   ├── cli.py          # Click CLI（run / chat / log）
│   ├── chat.py         # ChatSession 跨轮持久化
│   ├── renderer.py     # TUI 渲染（流式 Markdown + 工具块）
│   └── github_issue.py # GitHub Issue → PR 自动化
│
├── config/
│   ├── default.yaml    # 默认配置（引用 ${ENV_VAR}）
│   └── schema.py       # 配置加载 + 环境变量展开
│
├── .env.template       # 环境变量模板（提交到 git）
├── .env                # 本地配置（不提交，填入真实 key）
└── tests/              # 677+ 测试用例
```

---

## 核心特性

### 三层记忆系统

| 层级 | 存储 | 用途 |
|------|------|------|
| 短期记忆 | 对话历史（内存） | 当前会话上下文，滑动窗口 + 多层压缩 |
| 长期记忆 | .md 文件 | 跨会话持久化，按 name 精确读取 |
| 外部记忆 | SQLite + 向量索引 | RAG 语义检索，自动分块 + 主动召回 |

外部记忆 RAG 管线：
```
写入记忆 → 自动分块(chunker) → 批量 embed(fastembed) → SQLite memory_chunks
                                                              ↑
每轮对话 → user_message → ProactiveRetriever → search_chunks → 注入 LLM 上下文
```

### 多模型支持

- Anthropic Claude（原生 tool_use + prompt cache）
- OpenAI、DeepSeek、Groq、Ollama（OpenAI-compatible）
- 运行时 `/model` 切换，历史保留

### 安全机制（三层）

- **硬拦截**：`rm -rf /`、`mkfs` 等永不执行
- **只读白名单**：`ls`、`grep`、`git status` 等直接执行
- **写操作确认**：`--confirm` 模式需 y/n 确认

### 其他

- **流式输出**：thought + 工具调用实时渲染
- **Plan-and-Execute**：复杂任务 DAG 拆解
- **Docker 沙箱**：`--sandbox` 隔离执行
- **Reflection**：测试失败/死循环自动反思
- **Web 工具**：搜索 + URL 抓取 + SSRF 防护
- **MCP 协议**：外部工具服务器热插拔

---

## 多 Agent 架构（规划中）

当前 Forge Agent 是单 Agent 架构：一个 ReAct 循环驱动所有工具调用。后续将引入多 Agent 协作，设计原则：

### 设计思路

参考 Claude Code 的多 Agent 模式，通过 **模式切换** 方式引入（类似 `/mode plan`）：

```bash
# chat 中切换到多 Agent 模式
/mode multi-agent

# 或直接启动
python -m entry.cli chat --mode multi-agent
```

### 计划支持的 Agent 类型

| Agent 类型 | 模型 | 工具权限 | 用途 |
|-----------|------|---------|------|
| **Coordinator** | 主力模型 | 全部 | 任务分解、结果汇总、最终决策 |
| **Explorer** | 轻量模型 | 只读 | 快速代码搜索、文件定位 |
| **Coder** | 主力模型 | 读写 | 代码编写、文件修改 |
| **Reviewer** | 主力模型 | 只读 | 代码审查、方案评估 |
| **Tester** | 主力模型 | Shell | 运行测试、验证结果 |

### 通信架构

```
用户输入
    │
    ▼
Coordinator (主 Agent)
    ├── spawn Explorer → 搜索代码 → 返回结果
    ├── spawn Coder    → 修改文件 → 返回 diff
    ├── spawn Reviewer → 审查修改 → 返回建议
    └── spawn Tester   → 运行测试 → 返回状态
    │
    ▼
汇总结果 → 回复用户
```

### 核心设计原则

1. **星型拓扑**：子 Agent 只向 Coordinator 报告，互不通信
2. **上下文隔离**：每个子 Agent 独立 context window，避免污染
3. **模式统一**：和 `react` / `plan` 平级，通过 `/mode` 或 `--mode` 切换
4. **模型分层**：Explorer 用轻量模型（快+省），Coder/Reviewer 用主力模型（准）
5. **工具权限隔离**：Explorer 只读、Tester 只能跑 shell、Coder 才能写文件
6. **渐进式**：单 Agent 模式完全保留，多 Agent 是增强而非替代

### 实现路径

```
Phase 1: SubAgent 基础设施
  - SubAgent 数据模型（prompt / tools / model / isolation）
  - SubAgent 执行器（spawn → run → collect result）
  - 结果序列化（子 Agent 最终消息作为工具返回值）

Phase 2: Coordinator Agent
  - 任务分解策略（何时该 spawn 子 Agent vs 自己做）
  - 结果汇总 + 冲突解决
  - 注册为 /mode multi-agent

Phase 3: 并行执行
  - 多个子 Agent 并发执行（asyncio / threading）
  - Git worktree 隔离（并行文件修改不冲突）
  - Token 预算在子 Agent 间分配

Phase 4: 持久化 & 高级
  - 子 Agent 记忆持久化
  - Agent 间消息传递（team 模式）
  - Workflow 脚本编排（代码驱动的确定性编排）
```

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest                     # 全量（677 passed, 7 skipped）
pytest tests/test_rag_memory.py  # RAG 管线测试
pytest tests/test_day3.py  # 工具层测试

# 可选：更多语言的 tree-sitter 支持
pip install tree-sitter-javascript tree-sitter-typescript \
            tree-sitter-go tree-sitter-rust tree-sitter-java

# 可选：精确 token 计数
pip install tiktoken
```

---

## 命令参考

```bash
# chat（交互对话）
python -m entry.cli chat [--repo PATH] [--model MODEL] [--sandbox] [-v]

# run（一次性任务）
python -m entry.cli run --task TEXT [--repo PATH] [--task-file FILE]
          [--model MODEL] [--confirm] [--sandbox] [--no-stream] [-v]

# log（事件日志查看）
python -m entry.cli log list [--dir DIR]
python -m entry.cli log show LOG_FILE

# github issue（自动修复）
python -m entry.github_issue \
    -r owner/repo -i ISSUE_NUM -l LOCAL_PATH [--no-pr] [-v]
```
