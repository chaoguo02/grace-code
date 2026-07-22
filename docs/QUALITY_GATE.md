# Grace-Code CI 质量门禁

> **版本**: 1.1 (Phase 7 Batch C) | **日期**: 2026-07-22
> **状态**: 生效中 | **执行方式**: `bash tools/_quality_gate.sh`
> **门禁断言**: 15 (10 base + 2 CSS/E2E + 1 visual + 1 langfuse + 1 ssot)
> **Phase 7 Batch C key addition**: CSS-LINT, E2E-LIFECYCLE, VISUAL-DIFF, LANGFUSE-HEALTH

---

## 1. 门禁矩阵

| 门禁 ID | 维度 | 检查内容 | 阻断级别 | 自动化 |
|---------|------|---------|---------|--------|
| **ACC-1** | 循环依赖 | `importlib.import_module` 对 `agent.loop.*` / `agent.constants` / `chat_pipeline` 无循环 | **阻断** | `_quality_gate.sh` |
| **ACC-2** | 类型+文档 | ChatPipeline 公共方法有 docstring | **限速** | `_quality_gate.sh` (existential check) |
| **ACC-3** | 零裸魔数 | agent/core.py 中无 `3000`/`8000`/`32000` 等裸数字 | **阻断** | `_quality_gate.sh` |
| **ACC-4** | 状态安全 | `_stats_lock` + `_counter_lock` 存在 | **阻断** | `_quality_gate.sh` |
| **ACC-5a** | XSS 防护 | 所有 `dangerouslySetInnerHTML` 需经过 `renderMarkdownSafe` | **阻断** | `_quality_gate.sh` |
| **ACC-5d** | TS 零错误 | `npx tsc --noEmit` 0 errors | **阻断** | `_quality_gate.sh` |
| **ACC-6** | 性能基线 | 56 单元测试 + Session List ~30ms (基准测试) | **阻断** | `_quality_gate.sh` + CI |
| **L-3** | WS 合约 | `new WebSocket()` 仅存在于 `useWebSocket.ts` | **阻断** | `_quality_gate.sh` |
| **L-4** | E2E 框架 | 无独立 `subprocess.Popen` 在 E2E 测试中 | **限速** | `_quality_gate.sh` |

> **阻断** = 未通过不可合并。  
> **限速** = 未通过警告合并, 下一次 PR 修复。

---

## 2. 新增功能预检清单 (PR Template)

每个 PR 的 description 中必须选择以下 checklist 的适用项:

```
### Pre-merge Checklist

[ ] 56 unit tests passed (pytest)
[ ] npx tsc --noEmit = 0 errors
[ ] No raw magic numbers in new code (agent/core.py checks)
[ ] No new dangerouslySetInnerHTML sites added
[ ] New WS messages routed through connectWebSocket (not raw WebSocket)
[ ] E2E tests use ServerContext (not standalone subprocess)
[ ] RetryMetrics callback wired if observability enabled
[ ] /api/config/models SSOT unchanged — or sync verified
```

---

## 3. CI 集成

### GitHub Actions (example)

```yaml
# .github/workflows/quality-gate.yml
name: Quality Gate
on: pull_request
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: bash tools/_quality_gate.sh
```

### 本地执行

```bash
bash tools/_quality_gate.sh
```

---

## 4. 降级策略

若紧急热修复 (hotfix) 需绕过门禁, 需满足以下条件:

1. 热修复 PR 标注 `HOTFIX: bypass quality gate` 标签
2. 绕过项记录在 commit message body 中, 注明原因
3. 回录 PR (backfill PR) 在 48h 内打开, 修复绕过项
4. 任何门禁连续绕过超过 2 个 PR → 门禁等级升为阻断 (禁止继续绕过)

---

*本门禁文档的修改需通过 PR + 质量门禁自检通过。*
