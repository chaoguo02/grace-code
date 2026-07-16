# forge-agent CC 对齐 — run 模式测试

每个命令独立运行，互不干扰，无上下文累积。

---

## 测试 1: Read — CC 命名 + offset/limit

```bash
python -m entry.cli run --repo . --agent build --model mock --task "用 Read 工具读一下 pyproject.toml 的前 15 行"
```

预期: 工具调用显示 `Read`（不是 `file_read`），带上 `offset: 1, limit: 15`

---

## 测试 2: Grep — CC 参数

```bash
python -m entry.cli run --repo . --agent build --model mock --task "用 Grep 在 tools/ 目录的 *.py 文件中搜索 'BaseTool'，只列出文件名不要内容"
```

预期: `Grep` 工具, `output_mode: files_with_matches`, `glob: *.py`

---

## 测试 3: Glob — CC 命名

```bash
python -m entry.cli run --repo . --agent build --model mock --task "找出项目中所有 SKILL.md 文件"
```

预期: 调用 `Glob`（不是 `find_files`），pattern = `**/SKILL.md`

---

## 测试 4: WebSearch — CC 命名 + allowed_domains

```bash
python -m entry.cli run --repo . --agent build --model mock --task "搜索 Python asyncio 文档，只看 docs.python.org"
```

预期: `WebSearch`（不是 `web_search`），`allowed_domains: ["docs.python.org"]`

---

## 测试 5: Bash — CC 命名 + 参数

```bash
python -m entry.cli run --repo . --agent build --model mock --task "用 Bash 看看当前目录有哪些文件，timeout 10 秒"
```

预期: `Bash`（不是 `shell`），`command: ls`, `timeout: 10`

---

## 测试 6: 确认不再有旧工具名

```bash
python -m entry.cli run --repo . --agent build --model mock --task "查看 git 状态"
```

预期: `git_status` 工具被调用。注意 agent 不应该尝试调用 `git status` shell 命令（这是 CC 的做法），而是用我们的 `git_status` 工具。

---

## 测试 7: Skill 工具名

```bash
python -m entry.cli run --repo . --agent build --model mock --task "做一下代码审查"
```

预期: 如果 agent 决定用 skill，应该调用 `Skill(skill_name="code-review")`（不是 `use_skill`）

---

## 测试 8: MCP 命令（不启动 agent，直接测 CLI）

```bash
python -m entry.cli mcp list
python -m entry.cli mcp add --transport stdio test-svr -- echo hello
python -m entry.cli mcp get test-svr
python -m entry.cli mcp remove test-svr
```

预期: list 显示服务器列表，add/get/remove 正常读写 .mcp.json

---

## 测试 9: 综合 — 所有 CC 工具在同一个 run 中

```bash
python -m entry.cli run --repo . --agent build --model mock --task "
1. 用 Read 读 tools/file_tool.py 前 30 行
2. 用 Grep 在 tools/ 搜索 'ToolResult'，显示匹配行
3. 用 Glob 找所有 *.py 文件
4. 用 git_status 看状态
"
```

预期: 所有工具使用 CC 规范名。无报 unknown tool 错误。
