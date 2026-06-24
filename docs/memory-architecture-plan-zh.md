# 记忆系统架构方案

## 1. 架构原则

### 1.1 两层物理架构

```
┌──────────────────────────────────────────────────────────────┐
│  短期记忆（会话级）                                              │
│  ConversationHistory + Compaction + TokenBudget                │
│  ─────────────────────────────────────                        │
│  当前任务的完整对话上下文。滑动窗口管理，超出窗口的由 compaction   │
│  压缩为摘要。任务结束时清除。不依赖 SQLite、向量库或索引。         │
├──────────────────────────────────────────────────────────────┤
│  长期记忆（持久化，跨会话）                                      │
│  MemoryStore（文件） + ExternalMemoryStore（SQLite + fastembed）│
│  ─────────────────────────────────────                        │
│  三种记忆类型（同一套存储，不同检索策略）：                         │
│                                                                │
│  EPISODIC（情景）│ SEMANTIC（语义） │ PROCEDURAL（程序）          │
│  发生了什么       │ 什么是真的        │ 怎么做                    │
│  时间检索         │ 语义检索          │ 场景精确匹配              │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 "工作记忆"到底是什么

在 LLM agent 中，"工作记忆"（Working Memory）**不是一个独立的记忆模块**。
它就是在推理时刻模型能看到的全部消息数组：

```
[system prompt]        ← 永久规则
[long-term context]    ← 任务开始时注入一次
[conversation history] ← 短期记忆（由 compaction 管理）
[task anchor]          ← 每步注入：当前在做什么？
```

每步注入的 `task anchor + mode + policy` 属于 **prompt engineering**，
不属于记忆子系统。它的作用是：当 compaction 把原始任务消息从 history 中裁剪掉后，
确保模型仍然知道当前任务是什么。

### 1.3 三种记忆类型的理论依据

episodic / semantic / procedural 的三分法，有坚实的学术基础：

| 来源 | 贡献 |
|---|---|
| Tulving (1972) | 人类认知中情景-语义记忆的原始区分 |
| Anderson ACT-R | 数学激活/衰减模型：A_i = B_i + Σ(W_j·S_ji) |
| CoALA (arXiv:2309.02427, 2023) | 将 Tulving 模型正式映射到 LLM agent |
| "Memory in the Age of AI Agents"（2025 年，47 位作者，含 Google DeepMind、Stanford、Yale） | 该领域的权威综述，以 factual/experiential/working 为功能类别 |
| LangMem SDK (2025) | 直接实现：EpisodicMemory、SemanticMemory、ProceduralMemory |
| MongoDB Agent Memory Guide (2025) | 业界参考：episodic、semantic、procedural、associative |

需要强调的是：这不是三个独立的存储后端，而是**同一套持久化层上的三种检索策略**，
区别在于触发方式和检索方法。

---

## 2. 三种记忆类型的详细定义

### 2.1 情景记忆（Episodic）

**认知定义**（Tulving）：带有时间戳的亲身经历。"什么时间、什么场景、发生了什么。"

**在编码 agent 中**：
- 特定工具调用、其输出结果及上下文记录
- 例："`pytest test_plan_mode.py::test_edit_scope_blocks_other_file_reads`
  于 2025-06-23 在第 306 行因 AssertionError 失败"
- 例："读取了 `agent/core.py` 第 1140-1230 行，确认 `_run_planning_phase`
  将 `policy` 传递给了 `_run_execution_phase`"

**存储**：MemoryStore 中的完整内容（含时间戳、文件锚点、工具上下文）+
ExternalMemoryStore 中的向量嵌入。

**检索**：以文件/符号锚点 + 时间远近为主，语义相似度为辅。遵循 ACT-R 激活衰减：
访问越频繁的情景记忆，衰减越慢。

**生命周期**：
- 形成：任务完成时从 EventLog 自动提取（阶段 2）
- 巩固：相似情景合并为语义知识（阶段 3）
- 衰减：艾宾浩斯曲线：R(t) = e^(-t/S)，S 取决于重要性
- 过期：超过 N 天未被访问的情景记忆被清理（阶段 5）

### 2.2 语义记忆（Semantic）

**认知定义**（Tulving）：去除了上下文的常识和概念。"什么是普遍为真的，
与何时学到无关。"

**在编码 agent 中**：
- 项目知识：文件职责、模块关系、配置值
- 例："`agent/core.py` 包含 `ReActAgent`（主循环）和
  `PlanExecuteAgent`（规划-执行编排器）"
- 例："项目使用 `config/default.yaml`，通过
  `config/schema.py::load_config()` 加载"

**存储**：紧凑的事实陈述，含实体链接（文件路径、符号名）。
向量嵌入用于语义搜索。不依赖时间戳。

**检索**：以语义搜索（余弦相似度）为主，叠加关键词增强。
在任务开始时作为长期记忆上下文注入。

**生命周期**：
- 形成：从 EventLog 和用户交互中自动提取。也从情景记忆中巩固而来（阶段 3）
- 更新：出现矛盾证据时，更新而非复制
- 衰减：比情景记忆更慢。访问频率可阻止衰减
- 过期：仅在明确矛盾或关联文件被删除时

### 2.3 程序记忆（Procedural）

**认知定义**（Tulving 扩展，Anderson ACT-R）：技能、惯例和行为模式。
"如何做事情。"

**在编码 agent 中**：
- 从用户纠正中提取的精确行为规则
- 例："处理 YAML 配置文件时，应使用 `yaml.safe_load()` 而非正则解析"
- 例："修改 `agent/core.py` 中 FINISH 路径时，必须同步更新
  `CompletionValidator.validate()`"
- 例："修改 `agent/policy.py` 前，先读 `agent/policy_registry.py` ——
  两者紧密耦合"

**存储**：规则文本 + 强制文件/符号锚点。高重要性标记。
不主动过期，除非显式失效。

**检索**：**不使用语义搜索**。锚点精确匹配 + 任务类型激活。
当 agent 读取 `agent/core.py` 时，所有锚定到该文件的程序记忆自动注入。

**生命周期**：
- 形成：从用户纠正和重复模式中提取（阶段 2）
- 验证：锚定文件改动时，规则标记为待验证（阶段 5）
- 过期：仅在用户显式否定或文件验证证明规则不再适用时

---

## 3. 检索策略矩阵

|  | 情景记忆 | 语义记忆 | 程序记忆 |
|---|---|---|---|
| **触发时机** | 访问文件/符号时 | 任务开始时 | 访问文件/符号时 |
| **检索方式** | 锚点匹配 + 时间排序 | 语义搜索（余弦） | 锚点精确匹配 |
| **注入位置** | 可选（相关时每步注入） | 任务开始时（长期上下文） | 锚点命中时每步注入 |
| **数量限制** | 时间最近的前 3 条 | 相似度最高的前 5 条 | 所有匹配（预计很少） |
| **降级策略** | 无锚点时用语义搜索 | 余弦低时用关键词搜索 | 无 |

---

## 4. 分阶段执行计划

### 阶段 1：记忆类型系统 + 文件锚点

**借鉴对象**：LangGraph Store 的 namespace 设计、Letta Memory Blocks 的类型区分

#### Step 1.1：`memory/models.py` — Anchor 和类型枚举

改动：

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Any

# 合法的记忆类型
MemoryType = Literal["episodic", "semantic", "procedural"]

# 旧→新映射
_LEGACY_TYPE_MAP: dict[str, str] = {
    "user": "episodic",
    "feedback": "procedural",
    "project": "semantic",
    "reference": "semantic",
}

@dataclass
class Anchor:
    """记忆锚点：将记忆关联到文件、符号或任务类型。"""
    kind: str                   # "file" | "symbol" | "task"
    path: str | None = None     # 文件路径（用于 file/symbol 类型）
    name: str | None = None     # 符号名（用于 symbol 类型）
    value: str | None = None    # 任务类型关键词（用于 task 类型）

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k in ("kind", "path", "name", "value")
                if (v := getattr(self, k)) is not None}

@dataclass
class MemoryMetadata:
    """记忆元数据。"""
    type: str = "semantic"  # "episodic" | "semantic" | "procedural"

@dataclass
class Memory:
    name: str
    description: str
    content: str
    metadata: MemoryMetadata = field(default_factory=MemoryMetadata)
    updated_at: str = field(default_factory=lambda: _now())
    anchors: list[Anchor] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.metadata.type,
            "updated_at": self.updated_at,
            "content": self.content,
            "anchors": [a.to_dict() for a in self.anchors],
        }
```

#### Step 1.2：`memory/store.py` — frontmatter 和 file IO 适配

改动：

1. `_build_frontmatter(memory)` 增加锚点序列化
2. `read_memory(name)` 中解析锚点、调用旧类型映射
3. `_GLOBAL_MEMORY_TYPES` 更新 — 新类型语义下不再按类型分流全局/项目，改为全部存项目级

```python
# 锚点序列化到 frontmatter
def _build_frontmatter(memory: Memory) -> str:
    fm = {
        "name": memory.name,
        "description": memory.description,
        "metadata": {"type": memory.metadata.type},
        "updated_at": memory.updated_at,
    }
    if memory.anchors:
        fm["anchors"] = [a.to_dict() for a in memory.anchors]
    return yaml.dump(fm, ...)

# read_memory 中解析锚点 + 向后兼容
def read_memory(self, name: str) -> Memory | None:
    ...
    fm, body = _parse_frontmatter(text)
    meta = fm.get("metadata", {})
    raw_type = meta.get("type", "semantic")
    memory_type = _LEGACY_TYPE_MAP.get(raw_type, raw_type)

    anchors = []
    for a in (fm.get("anchors") or []):
        if isinstance(a, dict):
            anchors.append(Anchor(**a))

    return Memory(name=..., ..., metadata=MemoryMetadata(type=memory_type), anchors=anchors)
```

4. `TwoTierMemoryStore._GLOBAL_MEMORY_TYPES` 清空或删除 — 新类型下全部分流到项目级

> 注意：`TwoTierMemoryStore` 的全局/项目分流逻辑依赖旧类型（`user/feedback` → 全局）。新模型下所有记忆都存项目级，全局层保留但不新增。

#### Step 1.3：`memory/context.py` — 按类型分组

改动：`_build_filtered_section()` 中按类型分组输出：

1. 先展示程序记忆（`type == "procedural"`），标注 `### Rules to follow`
2. 再展示语义记忆（`type == "semantic"`），标注 `### Project knowledge`
3. 最后展示情景记忆（`type == "episodic"`），标注 `### Recent activities`

相关性评分逻辑不变，仅输出分组顺序按类型优先级调整。

#### Step 1.4：`tools/memory_tool.py` — 工具 schema 更新

改动：

1. `_TYPE_DESCRIPTIONS` 替换为新类型枚举描述
2. `memory_write` 的 `type` 枚举改为 `"episodic", "semantic", "procedural"`
3. 新增 `anchors` 可选参数（list of objects）
4. `execute()` 中验证 anchors 并构造 Anchor 对象

```python
_TYPE_DESCRIPTIONS = {
    "episodic": "What happened — tool calls, test outcomes, decisions made",
    "semantic": "What is true — project conventions, file responsibilities, config values",
    "procedural": "How to do things — user corrections, coding rules, patterns to follow or avoid",
}

# parameters_schema 新增
"anchors": {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["file", "symbol", "task"]},
            "path": {"type": "string"},
            "name": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["kind"],
    },
    "description": "Optional. Link this memory to specific files, symbols, or task types for targeted retrieval."
}
```

#### Step 1.5：`test_plan_mode.py` — 新增测试

三个测试用例：

```python
def test_memory_types_episodic_semantic_procedural():
    """三种类型均可创建和读取"""
    from memory.models import Memory, MemoryMetadata, Anchor
    m = Memory(name="test-mem", description="desc", content="body",
               metadata=MemoryMetadata(type="procedural"),
               anchors=[Anchor(kind="file", path="agent/core.py")])
    assert m.metadata.type == "procedural"
    assert m.anchors[0].path == "agent/core.py"

def test_memory_anchors_roundtrip(tmp_path):
    """锚点写入文件后读取不变"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata, Anchor
    import tempfile, os
    d = tempfile.mkdtemp()
    try:
        store = MemoryStore(repo_path="test", memory_dir=d)
        m = Memory(name="anchored-rule", description="...", content="...",
                   metadata=MemoryMetadata(type="procedural"),
                   anchors=[Anchor(kind="file", path="agent/core.py"),
                             Anchor(kind="task", value="refactoring")])
        store.write_memory(m)
        loaded = store.read_memory("anchored-rule")
        assert loaded is not None
        assert loaded.anchors[0].path == "agent/core.py"
        assert loaded.anchors[1].value == "refactoring"
    finally:
        import shutil; shutil.rmtree(d, ignore_errors=True)

def test_memory_backward_compat_old_types(tmp_path):
    """旧类型记忆读出来自动映射为新类型"""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "old-mem.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nname: old-mem\ndescription: test\nmetadata:\n  type: feedback\nupdated_at: 2025-01-01T00:00:00Z\n---\n\nold content\n")
        store = MemoryStore(repo_path="test", memory_dir=d)
        mem = store.read_memory("old-mem")
        assert mem is not None
        assert mem.metadata.type == "procedural"  # feedback → procedural
    finally:
        shutil.rmtree(d, ignore_errors=True)
```

#### 验证

```powershell
python -m pytest test_plan_mode.py -k "memory_type or anchor or backward_compat" -v
python -m compileall memory tools
```

#### 反思点

1. **锚点是关键基础设施**。没有它，阶段 4 的程序记忆精确检索无法实现。但阶段 1 只建数据模型，检索逻辑在阶段 4 才改 —— 这样每一步都可独立验证。
2. **向后兼容映射是单向的**。旧类型读出来自动映射，但新写进去的一定是新类型。不会同时存在两种类型名。
3. **`TwoTierMemoryStore` 的全局分流要停用**。旧逻辑把 `user/feedback` 存全局是为了跨项目共享用户偏好。新模型下所有类型都存项目级，全局层保留但不再写入新内容。存量全局记忆仍然可读。
4. **不需要数据库迁移脚本**。旧文件读的时候自动映射，新文件直接写新格式。存量记忆零破坏。

---

### 阶段 2：自动提取管线（形成期）

（阶段 1 完成后详细展开）

### 阶段 3：记忆合并去重（巩固期）

（阶段 2 完成后详细展开）

### 阶段 4：差异化检索

（阶段 3 完成后详细展开）

### 阶段 5：记忆验证与过期

（阶段 4 完成后详细展开）

### 阶段 6：集成与清理

（阶段 5 完成后详细展开）

---

## 5. 我们明确不做的事

| 特性 | 原因 |
|---|---|
| 后台 sleep-time agent | 编码 agent 不是长期运行的服务 |
| 知识图谱（Neo4j/Neptune） | 文件→符号映射已有 `repo_map` |
| 多模态记忆（图片/音频） | 纯代码和文本场景 |
| RL 训练的记忆策略 | 单人使用场景，无训练数据管线 |
| 分布式存储（PostgreSQL/Redis/MongoDB） | 单人使用，SQLite + 文件已足够 |
| "工作记忆"作为独立模块 | 上下文窗口本身就是工作记忆 |

---

## 6. 阶段间依赖关系

```
阶段 1（类型 + 锚点）──▶ 阶段 2（提取）──▶ 阶段 3（合并去重）
                                             │
阶段 4（检索）◀── 阶段 1（程序记忆触发需要锚点）
阶段 4（检索）◀── 阶段 3（需要去重后的记忆）
阶段 5（验证）◀── 阶段 1（文件失效需要锚点）
阶段 5（验证）◀── 阶段 2（需要验证的是自动创建的记忆）
阶段 6（集成）◀── 所有前置阶段

推荐执行顺序：1 → 2 → 3 → 4 → 5 → 6
```

---

## 7. 成功指标

| 指标 | 目标 | 衡量方式 |
|---|---|---|
| 类型准确性 | 程序记忆始终带有锚点 | `consolidate()` 中的断言检查 |
| 去重率 | >90% 的重复事实被识别 | 阶段 3 测试中记录 ADD/NOOP 比例 |
| 程序记忆触发精度 | >80% 的触发规则是相关的 | 阶段 6 手动 CLI 审查 |
| 提取噪音 | <30% 的自动提取记忆后续被清理 | 跟踪提取数量与后续清理数量的比值 |
| 记忆占用的 token 预算 | 长期上下文 < history 预算的 15% | `_build_messages` 中估算 |
| 失效捕获率 | >50% 文件修改后的程序规则被标记 stale | 手动 CLI 审查 |
