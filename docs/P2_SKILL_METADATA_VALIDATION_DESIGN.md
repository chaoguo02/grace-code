# P2: SkillMetadata 强类型校验 — CC-Native 设计

> 版本: v1.0 | 日期: 2026-08-01
> 对标: CC skillcheck 36-rule conformance + BuildStream safe bool + Pydantic model_validate
> 状态: Step 1-2 完成, 代码基线已存在, 即将进入 Step 3 审计

---

## 1. 调研与质询

### 1.1 搜索摘要

**A. CC Skill 验证生态**

CC 组件用两层字段模型：
- **Agent Skills 标准字段**: `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools` — 40+ runtime 通用
- **CC 扩展字段**: `model`, `context`, `agent`, `hooks`, `user-invocable`, `argument-hint`, `disable-model-invocation` 等

第三方工具的严格度分层：
- **skillcheck (npm)**: 36 规则, 严格层 — 未知字段 **报错**
- **skillport**: 允许 CC 2.1.0+ keys 的白名单
- **harn_vm**: 未知字段 → warning, 不阻断
- **agnix_core**: per-client 规则, CC 接受所有字段

**CC 自身行为**: CC CLI 不强制校验 frontmatter schema — 不支持的字段被静默忽略, 文件解析失败也**无 UI 错误**。

**B. YAML Boolean 陷阱**

PyYAML 实现 YAML 1.1 (不是 1.2), 自动 coercion:
- `true`/`True`/`TRUE` → `True`
- `false`/`False`/`FALSE` → `False`
- `yes`/`no`/`on`/`off` → bool (1.1 特有)

但前端通过 YAML parser 后, `bool("false")` 在 Python 里永远是 `True` — 因为非空字符串为 truthy。BuildStream 的修复是标准范式：显式字符串匹配 `"true"`/`"false"`，其他值抛 `ValueError`。

**C. Enum 验证**

enum 类字段需要精确值检查：
- `model`: `inherit`, `opus`, `sonnet`, `haiku` (CC)
- `effort`: `low`, `medium`, `high`, `xhigh`, `max` (CC)
- `context`: `fork` (唯一有效值)

### 1.2 质询应答

**Q1: CC 在 Skill 验证上的核心设计哲学是什么？**

CC 的哲学是 **"宽进严出"** — 解析阶段宽容 (未知字段静默忽略)，执行阶段严格 (无效字段在运行时产生可观察的行为差异)。这与我们的设计 (warning + reject invalid enum) 一致。

**Q2: 当前实现与 CC 的根本差异是`实现细节`还是`架构范式`？**

实现细节。`bool(fm_dict.get("disable-model-invocation", False))` 是类型转换 bug，不是架构问题。修复不需要重新设计 Skill 模型，只需要安全的类型转换。

**Q3: 硬性阻碍？**

无。Python 的 YAML parser 已经做了第一层类型转换 (true/false → bool)，我们的 `_parse_bool` 处理剩余两种情况：YAML string "false" 和 Python bool False。

**Q4: 隐式依赖？**

`_parse_bool` / `_parse_effort` / `_parse_context` 是纯函数 — 无 I/O, 无全局状态, 无外部依赖。

**Q5: 已知陷阱？**

1. YAML 1.1 把 `no` 转成 `False`, 但 `no` 在 CC frontmatter 中不是合法值 — 如果 SKILL.md 写 `disable-model-invocation: no`, YAML parser 返回 `False`, 这正是我们需要的。但如果有人想用 `no` 作为 tool name, 会被 PyYAML 破坏 — 这是 PyYAML 的问题, 解决方法是引用。
2. CC CLI 不报错 — 意味着用户可能以为 skill 生效但实际被静默忽略。我们的 warning 日志比 CC 更友好。
3. 未知字段报错 vs warning: CC 生态倾向于 warning, 我们的设计也采用 warning。

### 1.3 决策依据

当前代码有三处类型不安全:
1. `bool(fm_dict.get("disable-model-invocation", False))` — `"false"` → `True`
2. `bool(fm_dict.get("user-invocable", True))` — 同上
3. 无 effort/context enum 验证

修复方案: `_parse_bool` + `_parse_effort` + `_parse_context` + 未知字段 warning。已实现的代码方向正确, 需验证完整性。

---

## 2. 设计规范

### 2.1 接口契约

```python
def _parse_bool(value: Any, *, default: bool = False) -> bool:
    """Safe YAML bool conversion.

    Accepts:
      - Python bool → identity
      - str "true"/"1"/"yes"/"on" → True
      - str "false"/"0"/"no"/"off"/"" → False
      - int/float → bool(value) (0→False, non-zero→True)
      - None → default

    Explicitly rejects: nothing.  Unknown strings → default.
    This is intentionally permissive for CC-compatibility.
    """

def _parse_effort(value: Any) -> str:
    """Validate 'effort' against CC allowed set.
    Valid: "" | "low" | "medium" | "high" | "xhigh" | "max"
    Invalid → warning + return ""
    """

def _parse_context(value: Any) -> str:
    """Validate 'context' against CC allowed set.
    Valid: "" | "fork"
    Invalid → warning + return ""
    """
```

### 2.2 解耦矩阵

| 本函数 | Skill 执行 | Tool Registry | MCP | Session | HITL |
|--------|-----------|-------------|-----|---------|------|
| `_parse_bool` | 不感知 | 不感知 | 不感知 | 不感知 | 不感知 |
| `_parse_effort` | 不感知 | 不感知 | 不感知 | 不感知 | 不感知 |
| `_parse_context` | 不感知 | 不感知 | 不感知 | 不感知 | 不感知 |

### 2.3 未知字段处理

```
已知: name, description, when_to_use, disable-model-invocation,
      user-invocable, model, effort, context, agent,
      allowed-tools, disallowed-tools, paths, arguments,
      evidence, hooks

未知 → logger.warning("Unknown frontmatter field '%s' in %s", key, file)
       继续解析 (不阻断, CC-compatible)
```

---

## 3. 验收标准

- [ ] AC-1: `_parse_bool("false")` → `False` (不是 `True`)
- [ ] AC-2: `_parse_bool("true")` → `True`
- [ ] AC-3: `_parse_bool(True)` → `True`, `_parse_bool(False)` → `False`
- [ ] AC-4: `_parse_effort("invalid")` → `""` + WARNING 日志
- [ ] AC-5: `_parse_effort("high")` → `"high"`
- [ ] AC-6: `_parse_context("invalid")` → `""` + WARNING 日志
- [ ] AC-7: `_parse_context("fork")` → `"fork"`
- [ ] AC-8: 未知 frontmatter 字段 → WARNING 日志 (不抛异常)
- [ ] AC-9: 现有 7 个 skill 测试保持通过
