# forge-agent 开发任务清单

> 更新日期：2026-06-18

---

## 当前状态 v1.2

| 能力 | 状态 |
|------|------|
| 核心 ReAct 循环 | ✅ |
| Plan-and-Execute + DAG | ✅ |
| 三层记忆系统 (短/长/向量) + 主动记忆 | ✅ |
| RAG (AST code chunker + fastembed) | ✅ |
| Multi-Agent 协作 (Coordinator + 3 tiers) | ✅ |
| MCP 协议核心 (stdio + 工具代理) | ✅ |
| Web 工具 + SSRF 防护 | ✅ |
| TUI 渲染器 (InlineRenderer) | ✅ |
| 多模型 + 运行时切换 | ✅ |
| Skill 系统 (MVP: 发现/加载/SkillTool) | ✅ |
| HITL 完整框架 (RiskLevel + Policy + Manager) | ✅ |
| 结构化上下文 + Prompt Caching | ✅ |
| Shell 确认 + Docker 沙箱 | ✅ |

验证：**以 E2E_TEST_PLAN.md 手工端到端验证为主；当前仓库不再保留历史 pytest 测试目录**

---

## 🎯 下一阶段开发（三大模块）

### 优先级排序

```
P0: Prompt 分层架构   — 可维护性基础，影响后续所有 prompt 调优
P1: Skill 系统完善    — 依赖 Prompt 分层的"技能注入"机制
P2: MCP 高级能力      — resources/prompts/通知，扩展生态
```

---

## 一、Prompt 分层架构（第 19 期）

### 1.1 当前问题

- `agent/prompt.py` 单文件硬编码所有 prompt（476 行 Python 字符串）
- 修改 prompt 需要改源码、重启
- ReAct / Plan / Multi-Agent / SubAgent 四种模式各自拼装 prompt，逻辑分散
- 无法用户级覆盖（想调 prompt 就得 fork 代码）
- Prompt 内容与组装逻辑耦合

### 1.2 目标

把 prompt 从"代码中的字符串"变成"文件系统中的 Markdown"，支持分层组装 + 用户覆盖。

### 1.3 架构设计

```
prompts/                          ← 内置 prompt（随代码提交）
├── base.md                       ← 核心规则（工具使用、输出格式、安全约束）
├── modes/
│   ├── react.md                  ← ReAct 工作流规则
│   ├── plan.md                   ← Plan 模式规则（只读探索 + 执行）
│   ├── plan-dag.md               ← DAG Plan JSON 格式要求
│   ├── coordinator.md            ← Multi-Agent Coordinator 规则
│   └── sub-agent.md              ← SubAgent 通用规则
├── memory/
│   └── auto-memory.md            ← Auto Memory 使用指导
├── reflection/
│   ├── test-failed.md
│   ├── no-edit.md
│   └── loop-detected.md
└── context/
    └── task.md                   ← 任务描述模板

~/.forge-agent/prompts/           ← 用户级覆盖（同路径文件替换内置）
.forge-agent/prompts/             ← 项目级覆盖（最高优先级）
```

### 1.4 核心组件

```python
class PromptAssembler:
    """按固定顺序组装 prompt，支持分层覆盖。"""
    
    def assemble(self, mode: str, context: AssemblyContext) -> str | list[dict]:
        """
        组装顺序（稳定在前，volatile 在后 → 最大化 cache 命中）：
        1. base.md          — 核心规则（极稳定）
        2. modes/{mode}.md  — 运行模式规则（session 级稳定）
        3. memory section   — 记忆指导 + 记忆内容（轮次级变化）
        4. repo context     — repo_map + skills（session 级）
        5. task context     — 当前任务（轮次级）
        """
    
    def resolve_file(self, relative_path: str) -> str:
        """三层查找：项目级 → 用户级 → 内置。"""
```

### 1.5 实现子任务

- [ ] 创建 `prompts/` 目录结构，将 `prompt.py` 中的字符串拆为独立 .md 文件
- [ ] 实现 `prompt/assembler.py` — PromptAssembler（三层文件解析 + 模板变量替换）
- [ ] 实现三层覆盖逻辑：内置 → `~/.forge-agent/prompts/` → `.forge-agent/prompts/`
- [ ] 重构 `agent/core.py` — `_build_messages()` 调用 PromptAssembler 替代 prompt.py 函数
- [ ] 重构 Plan / Multi-Agent prompt 构建逻辑
- [ ] `prompt.py` 保留为薄兼容层（调用 PromptAssembler 的旧接口）
- [ ] 模板变量支持：`{repo_path}`, `{tool_descriptions}`, `{repo_summary}`, `{task_description}` 等
- [ ] 确保 Prompt Cache 友好：稳定前缀不因 mode 切换而变（base.md 永远在最前面）
- [ ] 测试：覆盖文件后 prompt 内容正确变化

### 1.6 Prompt Cache 设计要点

```
┌─────────────────────────────────────────────────────┐
│  base.md + modes/react.md + tool_descriptions       │ ← cache 稳定前缀
│  (这部分每轮不变，可命中 prompt cache)                │
├─────────────────────────────────────────────────────┤
│  memory section + repo_map + skills index           │ ← session 级稳定
├─────────────────────────────────────────────────────┤
│  conversation history + task context                │ ← 每轮变化
└─────────────────────────────────────────────────────┘
```

---

## 二、Skill 系统完善（第 15 期增强）

### 2.1 当前状态

已有骨架：
- `skills/registry.py` — SkillRegistry：扫描 `SKILL.md`，解析 frontmatter，`load_and_render()`
- `skills/tool.py` — SkillTool：LLM 可调用 `use_skill(name, arguments)` 加载 skill body
- CLI 入口已注册 SkillTool（有 skill 时）

缺失：
- 没有任何内置 skill 内容
- 没有 `/skill` 系列 CLI 命令（list / show / on / off）
- Skill body 注入上下文的机制未完善（一次性消费 buffer）
- 没有 skill 与 PromptAssembler 的联动（启用 skill 的 description 注入 system prompt 索引段）

### 2.2 目标

1. 内置 3-5 个实用 skill
2. 完善 skill 生命周期（发现 → 索引展示 → LLM 按需加载 → 注入上下文 → 一次性消费）
3. CLI 命令闭环

### 2.3 内置 Skill 清单

| Skill 名称 | 用途 | 触发场景 |
|-----------|------|---------|
| `code-review` | 代码审查手册（关注点、格式、checklist） | 用户说"帮我 review" |
| `debug-guide` | 调试方法论（二分法、日志注入、最小复现） | Agent 连续失败 / 用户说"帮我调试" |
| `git-workflow` | Git 操作规范（commit message、branch 策略） | 涉及 git 操作时 |
| `refactor` | 重构手册（提取方法、消除重复、SOLID） | 用户说"重构" |
| `test-writing` | 测试编写指南（单测结构、mock 策略、边界用例） | 用户说"补测试" |

### 2.4 Skill 注入机制

```
启动时:
  SkillRegistry.format_for_prompt() → 注入 system prompt 尾部
    "## Available Skills: code-review, debug-guide, ..."

运行时（LLM 按需）:
  LLM 调用 use_skill("code-review", "auth module") → SkillTool.execute()
    → SkillRegistry.load_and_render("code-review", "auth module")
    → 返回渲染后的 SKILL.md body 作为工具结果

下一轮 LLM 请求:
  工具结果（skill body）自然在 conversation history 中
  LLM 根据 skill 内容执行
```

### 2.5 实现子任务

- [ ] 创建 `.forge-agent/skills/code-review/SKILL.md`（内置示例 skill）
- [ ] 创建 `.forge-agent/skills/debug-guide/SKILL.md`
- [ ] 创建 `.forge-agent/skills/git-workflow/SKILL.md`
- [ ] 创建 `.forge-agent/skills/refactor/SKILL.md`
- [ ] 创建 `.forge-agent/skills/test-writing/SKILL.md`
- [ ] `entry/chat.py` — 添加 `/skill list` / `/skill show <name>` 命令
- [ ] Skill 索引注入 system prompt（通过 PromptAssembler 的 skills slot）
- [ ] SkillContextBuffer：限制同时加载 skill 数量（max 3），避免上下文膨胀
- [ ] 测试：skill 发现、加载、渲染、CLI 命令

### 2.6 SKILL.md 格式规范

```markdown
---
name: Code Review
description: 代码审查清单与最佳实践，帮助识别 bug、性能问题和可维护性问题
triggers:
  - review
  - 审查
  - check code
---

## Code Review Checklist

### Correctness
- [ ] Logic errors: off-by-one, null handling, edge cases
- [ ] Error handling: exceptions caught, resources cleaned up
...

### For: $ARGUMENTS
Focus your review on the specific code/module described above.
```

---

## 三、MCP 高级能力（第 11 期）

### 3.1 当前状态

已有：
- `tools/mcp_client.py` — MCPToolProxy + MCPServerManager
- stdio 传输（`mcp` Python SDK）
- `tools/list` + `tools/call` 工具发现与代理
- `config/default.yaml` — `mcp_servers: {}` 配置段
- 内置 `mcp_servers/web_search_server.py`

缺失：
- `resources/list` + `resources/read`（MCP 资源访问）
- `prompts/list` + `prompts/get`（MCP prompt 模板查看）
- `notifications`（被动通知处理：工具列表变更、资源更新）
- CLI 命令：`/mcp resources`, `/mcp prompts`, `/mcp restart`

### 3.2 目标

补齐 MCP 规范中 resources 和 prompts 能力，让 forge-agent 能：
1. 列出并读取 MCP server 提供的资源（文件、数据库条目等）
2. 查看 MCP server 提供的 prompt 模板
3. 响应 server 端的工具/资源变更通知

### 3.3 架构设计

```
MCPServerManager
  └── MCPServerConnection（每个 server 一个）
        ├── tools/list → MCPToolProxy 注册到 ToolRegistry
        ├── resources/list → MCPResourceProxy 注册为虚拟工具
        │     mcp__{server}__list_resources
        │     mcp__{server}__read_resource
        ├── prompts/list → CLI 展示（不注入对话）
        └── notifications handler
              tools/list_changed → 重新拉取工具列表
              resources/list_changed → 缓存失效
```

### 3.4 Resources 双轨设计（参考 Claude Code）

**工具层**（LLM 自决）：
- 每个支持 resources 的 server 自动注册 `mcp__{server}__list_resources` 和 `mcp__{server}__read_resource` 虚拟工具
- LLM 根据任务需要自行调用

**用户 @-mention 层**（显式引用）：
- 用户输入 `@server:protocol://path` 时，预处理阶段 fetch 资源内容
- 替换为 `<resource uri="...">content</resource>` 内联块注入 user message

### 3.5 实现子任务

- [ ] `tools/mcp_client.py` — 添加 `resources_list()` / `resources_read(uri)` 方法到 MCPServerConnection
- [ ] `tools/mcp_client.py` — MCPResourceProxy（BaseTool 子类）：`mcp__{server}__list_resources` / `mcp__{server}__read_resource`
- [ ] `tools/mcp_client.py` — 添加 `prompts_list()` / `prompts_get(name)` 方法
- [ ] `tools/mcp_client.py` — 通知处理：`tools/list_changed` → 重拉工具列表替换注册
- [ ] `tools/mcp_client.py` — 通知处理：`resources/list_changed` → 资源缓存失效
- [ ] `entry/chat.py` — `/mcp` 命令（显示已连接 server 状态）
- [ ] `entry/chat.py` — `/mcp resources <server>` 列出资源
- [ ] `entry/chat.py` — `/mcp prompts <server>` 列出 prompt 模板
- [ ] `entry/chat.py` — `/mcp restart <server>` 重启单个 server
- [ ] 用户输入预处理：`@server:uri` 语法解析 + 资源 fetch + 内联替换
- [ ] 测试：resources list/read mock、notification handler、CLI 命令

### 3.6 不做（明确边界）

- OAuth 2.0 认证（需要浏览器交互，复杂度高）
- `sampling/createMessage`（server 反向调用 LLM，安全风险大）
- MCP server 自动重启 / health ping
- resources 自动注入 system prompt（通过工具层 LLM 自决即可）

---

## 实现顺序

```
Week 1: Prompt 分层架构
  ├── Day 1-2: 创建 prompts/ 目录，拆解 prompt.py 为 .md 文件
  ├── Day 3-4: PromptAssembler 实现 + 三层覆盖
  └── Day 5:   重构 agent/core.py + 测试

Week 2: Skill 系统完善
  ├── Day 1-2: 编写 5 个内置 SKILL.md
  ├── Day 3:   /skill CLI 命令 + SkillContextBuffer
  └── Day 4-5: 与 PromptAssembler 联动 + 测试

Week 3: MCP 高级能力
  ├── Day 1-2: resources list/read + MCPResourceProxy
  ├── Day 3:   prompts list/get + notification handler
  └── Day 4-5: CLI 命令 + @mention 预处理 + 测试
```

---

## 已完成能力

| 期数 | 主题 | 状态 |
|------|------|------|
| 1 | 基础 ReAct + Tool Call | ✅ |
| 2 | Plan-and-Execute + DAG | ✅ |
| 3 | Memory + 上下文工程 | ✅ |
| 4 | RAG + 代码库理解 | ✅ |
| 5 | Multi-Agent 协作 | ✅ |
| 6 | HITL 完整框架 | ✅ |
| 7 | 异步 + 并行工具 | ✅ |
| 8 | 多模型适配 + 运行时切换 | ✅ |
| 9 | 联网能力 + Web 工具 | ✅ |
| 10 | MCP 协议核心 | ✅ |
| 12 | 结构化上下文 + Prompt Caching | ✅ |
| 15 | Skill 系统 (MVP) | ✅ |
| 16 | TUI 界面 + 产品化 | ✅ |
