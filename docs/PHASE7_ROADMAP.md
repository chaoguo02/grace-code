# Grace-Code Phase 7 Roadmap — 初始化与首轮迭代

> **版本**: 初稿, 待 Tech Lead + Product Owner 双签 | **日期**: 2026-07-22
> **状态**: Draft → Awaiting Sign-off
> **输入文件**: [LEGACY_OWNERSHIP.md](LEGACY_OWNERSHIP.md), [QUALITY_GATE.md](QUALITY_GATE.md), [RISK_REGISTER.md](RISK_REGISTER.md)
> **团队规模**: Solo Developer (guo)

---

## 1. 架构健康度可量化指标

| 指标 | 当前值 | Phase 7 目标 | 测量方法 | 频次 |
|------|--------|------------|---------|------|
| **ACC 合规率** | 6/6 (100%) | ≥98% | `tools/_quality_gate.sh` | 每个 PR |
| **E2E 覆盖率** | 1 script (D0 abort) | ≥85% of lifecycle paths | `ServerContext` 测试计数 | 每月 |
| **P2 新增速率** | 0 (Phase 6 closed 16) | ≤2/月 | `docs/TODO.md` P2 行数增长 | 每月 |
| **CI 门禁通过率** | 100% (首次脚本) | ≥95% | `tools/_quality_gate.sh` exit code | 每个 PR |
| **风险登记册复审及时率** | N/A (新建) | 100% (季度内完成) | `RISK_REGISTER.md` last-modified | 每季度 |

---

## 2. 四大遗产所有权与维护契约

> 引用 [LEGACY_OWNERSHIP.md](LEGACY_OWNERSHIP.md) §2 完整内容。此处仅摘要映射到批次。

| 遗产 | 所有者 | Phase 7 Batch | 激活里程碑 |
|------|--------|-------------|-----------|
| L-1 RetryMetrics→Langfuse | guo | A | Langfuse tracer merged |
| L-2 /api/config/models SSOT | guo | Kickoff | `tools/_check_ssot.sh` added to PR template |
| L-3 connectWebSocket 合约 | guo | Kickoff | grep audit passes (0 raw `new WebSocket`) |
| L-4 ServerContext E2E | guo | Kickoff | All new E2E PRs use `ServerContext` |

---

## 3. 批次规划草案

### Batch A (Week 1-2, ~8h): 门禁落地 + 观测激活

| 项 | 内容 | 类型 | ETA |
|----|------|------|-----|
| A-1 | `tools/_quality_gate.sh` 在 CI 中运行并阻断合并 | CI | Kickoff |
| A-2 | `PULL_REQUEST_TEMPLATE.md` 加入 Pre-merge Checklist | 流程 | Kickoff |
| A-3 | L-1: Langfuse tracer `on_metrics(RetryMetrics)` 实现 | 功能 | Week 1 |
| A-4 | L-2: `tools/_check_ssot.sh` 校验 `MODEL_CATALOG` ↔ `constants.py` | 流程 | Week 1 |
| A-5 | L-3: grep audit 确认 `new WebSocket()` 仅在 `useWebSocket.ts` | 流程 | Week 1 |
| A-6 | ACC-6 性能基线纳入 CI (p99 ≤500ms, Session List ~30ms) | CI | Week 2 |
| **可交付** | CI 门禁阻断合并、Langfuse 可见 retry metrics、PR template 强制执行 | | |

### Batch B (Week 3-4, ~8h): 遗留 P2 1项 + 测试扩展

| 项 | 内容 | 类型 | ETA |
|----|------|------|-----|
| B-1 | P2-26: SubagentDetail/SubagentProgress/SessionTree 的 inline styles → CSS 类 | 前端 UX | Week 3 |
| B-2 | E2E 覆盖率: `ServerContext` 扩展 2 个生命周期测试 (plan→approve→execute, session crash recovery) | 测试 | Week 4 |
| **可交付** | 3 组件 CSS 迁移完成、E2E 覆盖率提升至 3 脚本 | | |

### Batch C (Backlog, 按需): 依赖团队 Roadmap 输入动态调整

| 项 | 内容 | 触发条件 |
|----|------|---------|
| C-1 | 性能深化: `ChatView` React rendering Profiling | Session 数 > 100 或 report of sluggish UX |
| C-2 | Docker 沙箱集成 (R-3 升级路径) | 生产部署需求 |
| C-3 | Hook FAIL_CLOSED 集成测试 (R-2 升级路径) | Internal hook 新增 I/O 操作 |
| C-4 | 前端组件库提取 / Design System v0.1 | 新 UI 功能 > 3 组件 |
| C-5 | Web UI E2E with Playwright | 前端变更频率 > 2 PR/周 |

---

## 4. 风险登记册引用清单

> 引用 [RISK_REGISTER.md](RISK_REGISTER.md) 全部 4 项目。规划中受影响的批次标注。

| 风险 ID | 标题 | 影响批次 | 影响内容 |
|---------|------|---------|---------|
| R-1 | MicroCompactor 就地修改 | Batch C-3 | 若有新调用方 → 升级 FAIL_CLOSED |
| R-2 | Hook 异常静默 | Batch C-3 | Internal hook 新增 I/O → 升级 FAIL_CLOSED |
| R-3 | ROOT_REMOVAL 黑名单 | Batch C-2 | Docker 部署 → 文件系统级防护 |
| R-4 | Windows TOCTOU | Batch C-2 | Docker 部署 → 容器化隔离 |

> **规划原则**: 风险登记册中的项不作为主动修复目标 (均已评估为 LOW), 但相关模块变更时需重新评估。C-2/C-3 作为升级路径保留在 Backlog 中。

---

## 5. 签收与生效

| 角色 | 姓名 | 签收日期 | 备注 |
|------|------|---------|------|
| Tech Lead | guo | 2026-07-22 | Solo-dev: 自我签收 |
| Product Owner | — | — | 无专职 PO, road map 为自驱动 |

### 签收确认

- [x] 4 大遗产所有权已确认 (LEGACY_OWNERSHIP.md)
- [x] CI 质量门禁已设计 (QUALITY_GATE.md)
- [x] 风险登记册已初始化 (RISK_REGISTER.md)
- [x] 架构健康度指标已量化
- [ ] 首次复审日期已设定 (2026-10-22)

---

## 6. 生效后第一动作 (Phase 7 Kickoff Week 1)

1. `git add tools/_quality_gate.sh docs/QUALITY_GATE.md` → 启用 quality gate
2. `git add docs/LEGACY_OWNERSHIP.md docs/RISK_REGISTER.md` → 正式归档
3. 本地运行 `bash tools/_quality_gate.sh` → 确认全部 PASS
4. 创建 Langfuse tracer 分支 → 开始 Batch A-3 实现
5. 设置 2026-10-22 日历提醒 → RISK_REGISTER quarterly review

---

*本 Roadmap 在 Tech Lead 签收后 24 小时内生效。Phase 7 Kickoff = Batch A-1 执行。*
