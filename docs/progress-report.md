# 修复进度报告

> 基线: `0607d0c` (2026-07-17) → 当前: `b918730` (2026-07-17)
> 对照文档: `remediation-opinion-and-guidelines.md` + `elegance-remediation-plan-18-points.md`

---

## 一、18 Point 逐点状态

| Point | 内容 | 状态 | 批次 |
|-------|------|------|------|
| 1 | 源码编码污染 | 🔵 待处理 | — |
| 2 | agent.v2/agent.session 双命名空间 | ✅ | 迁移 Phase 1-4 + Batch A |
| 3 | 主入口依赖 agent.v2 | ✅ | Batch A: entry/ 0 残留 |
| 4 | Plan 双轨机制 | ✅ | A-6: _pending_mode_switch 接入主循环 |
| 5 | Plan 审批递归重入 build | 🔵 已评估 P3 | — |
| 6 | _pending_mode_switch 私有旗标 | ✅ | Batch E: mode_switching.py 提取 |
| 7 | context:fork 绕开 SessionRuntime | 🔵 已评估 | — |
| 8 | Skill modifier 语义 | ✅ | Batch B: _apply_skill_modifier 统一 |
| 9 | Subagent 超长 prompt | ✅ | Batch D: 80→25 行 |
| 10 | agent/core.py 巨石化 | ✅ 渐进 | Batch E + I: mode_switching + ProcessInvoker |
| 11 | Shell cmd legacy | ✅ | J: deprecated 已标记 |
| 12 | Shell 黑名单 | 🔵 不认同 | L0 纵深防御，不缩小 |
| 13 | 散落 subprocess.run | ✅ | Batch I+K: ProcessInvoker 全覆盖 |
| 14 | MCP bridge-first | 🔵 延后 | P2，中期工程 |
| 15 | MCP deferred schema | 🔵 延后 | P2 |
| 16 | 工具别名表 | 🔵 不认同 | 10 条永久兼容层 |
| 17 | legacy/fallback 残留 | 🔵 待审计 | — |
| 18 | 示例/边角代码 | ✅ | Batch H: executor/examples.py 已删 |

**总计: 10/18 完成, 5/18 已评估不处理, 3/18 延后**

---

## 二、8 Batch 对照 remediation-opinion-and-guidelines

| 计划 Batch | 内容 | 状态 | 对应批次 |
|-----------|------|------|---------|
| A | 主入口迁移 agent.session | ✅ | 迁移 Phase 1-4 + Batch A |
| B | Skill modifier 语义纠偏 | ✅ | C-2(K5) + Batch B |
| C | Plan orchestration 收口 | ✅ | Batch C: _plan_approval_loop() 提取 |
| D | Subagent contract 下沉 | ✅ | Batch D: _SUBAGENT_PROTOCOL 减重 |
| E | agent/core.py 拆职责 | ✅ 渐进 | Batch E: mode_switching.py |
| F | Shell + Process adapter | ✅ | Batch I+J+K: ProcessInvoker |
| G | MCP registry-first | 🔵 延后 | P2 |
| H | legacy/examples 清理 | ✅ 部分 | Batch H: examples.py 删除 |

**总计: 6/8 完成, 1/8 部分, 1/8 延后**

---

## 三、当前架构全貌

```
entry/         → CLI 入口 (0 处 agent.v2)
agent/session/ → V2 运行时 (canonical namespace)
agent/v2/      → __init__.py 兼容壳
agent/core.py  → ReActAgent (~1780 行, 下减中)
core/          → 基础设施 (BaseTool, PhasePolicy, ProcessInvoker)
executor/      → 进程执行 (ProcessInvoker, MCP, workspace)
```

**当前不处理**:
- L0 Shell 黑名单 (纵深防御, 不缩小)
- 工具别名表 (10 条永久兼容)
- Plan 递归 build (P3 优化, 非阻塞)
- MCP registry-first (P2 中期工程)
- OAuth/Elicitation/Channels (自托管无需求)
