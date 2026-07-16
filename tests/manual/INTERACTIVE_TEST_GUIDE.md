# forge-agent CC 对齐 — 交互式验证指南

启动命令（在项目根目录）：
    python -m entry.cli chat --repo .

然后在对话中输入以下测试问题，观察 agent 是否正确使用 CC 对齐的工具。

---

## 测试 1：工具命名 — 确认 agent 用了 CC 规范名

**输入**：
    帮我读一下 README.md，看看项目是做什么的。

**预期**：agent 调用 `Read` 工具（不是 `file_read` 或 `read_file`）。
观察工具调用里显示 `ToolCall [1] Read → README.md`

---

## 测试 2：Read 分页参数

**输入**：
    读 pyproject.toml 的前 20 行。

**预期**：agent 调用 `Read` 时带上 `offset: 1, limit: 20` 参数。
如果有大文件，要求 "从第 50 行开始读 30 行" → offset=50, limit=30。

---

## 测试 3：Grep 搜索模式

**输入**：
    在 tools/ 目录下搜索所有 Python 文件里包含 "BaseTool" 的地方，只列出文件名，不要内容。

**预期**：agent 调用 `Grep` 时使用 `output_mode: files_with_matches`，`glob: *.py`。
应该返回文件路径列表，不包含行内容。

再试：
    在 skills/ 目录下搜索 "registry"，显示匹配的行和行号。

**预期**：`output_mode: content`，返回 `registry.py:行号: 内容` 格式。

---

## 测试 4：Glob 文件查找

**输入**：
    找出项目中所有 SKILL.md 文件。

**预期**：agent 调用 `Glob` 工具，pattern = `**/SKILL.md`。
返回 skills/builtin/*/ 下的各个 SKILL.md 路径。

---

## 测试 5：Skill 斜杠命令

**输入**：
    /code-review tools/file_tool.py

**预期**：
1. 应该看到 `Skill 'code-review' activated...`
2. agent 直接开始审查 file_tool.py（不走 tool_use 往返）
3. 如果内置 code-review skill 有 triggers 残留也没关系——我们已删除了 triggers

再试 LLM 自主调用：
    帮我做一下代码审查。

**预期**：agent 可能通过 `Skill(skill_name="code-review")` 工具加载 skill。

---

## 测试 6：WebSearch（如果 ddgs 模块已安装）

**输入**：
    搜索一下 Python asyncio 的最新文档，只看 docs.python.org 的结果。

**预期**：agent 调用 `WebSearch`（不是 `web_search`），
可能带上 `allowed_domains: ["docs.python.org"]` 参数。

如果没有安装 ddgs，agent 会报错，这也是正常的——参数 schema 已经正确。

---

## 测试 7：Git 工具

**输入**：
    看看当前 git 状态，有没有未提交的改动。

**预期**：agent 调用 `git_status` 工具（不是 Bash git status）。
这是 forge-agent 独有的本地工具。

---

## 测试 8：EnterPlanMode（信号工具）

**输入**：
    我要做一个大的重构，先进入计划模式帮我设计一下方案。

**预期**：agent 可能调用 `EnterPlanMode` 工具。
工具返回成功后，下一轮 agent 应该切换到只读分析模式。

---

## 测试 9：Skill 参数替换

先创建一个测试 skill（如果 ~/.forge-agent/skills/ 不存在的话，在项目 .forge-agent/skills/ 下创建）：

    mkdir -p .forge-agent/skills/echo-args
    cat > .forge-agent/skills/echo-args/SKILL.md << 'EOF'
    ---
    name: Echo Args
    description: Echo back the arguments with substitutions
    arguments:
      - name
      - count
    ---
    Hello, $ARGUMENTS!
    Name: $name
    Count: $count
    Shorthand $0 = $1
    EOF

然后输入：
    /echo-args Alice 3

**预期**：skill 渲染后 agent 看到：
    Hello, Alice 3!
    Name: Alice
    Count: 3
    Shorthand Alice = 3

---

## 测试 10：MCP CLI 命令（不启动 agent）

直接在终端运行：

    python -m entry.cli mcp list
    python -m entry.cli mcp add --transport stdio test-server -- echo hello
    python -m entry.cli mcp get test-server
    python -m entry.cli mcp remove test-server

**预期**：list 显示已配置的服务器，add/get/remove 正常操作 .mcp.json。
