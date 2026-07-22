# Grace-Code 风险登记册

> **版本**: 1.0 | **日期**: 2026-07-22
> **状态**: 生效中 | **首次复审**: 2026-10-22
> **复审周期**: 每季度 (Jan/Apr/Jul/Oct)

---

## 1. 登记项总览

| ID | 风险标题 | 严重度 | 当前评级 | 首次复审 | 关联 P2 |
|----|---------|--------|---------|---------|---------|
| R-1 | MicroCompactor 就地修改输入列表 | LOW | 接受 | 2026-10-22 | P2-36 |
| R-2 | Hook 异常静默吞没 (blockable 路径) | LOW | 接受 | 2026-10-22 | P2-38 |
| R-3 | `_ROOT_REMOVAL_PATTERNS` 黑名单可绕过 | LOW | 接受 | 2026-10-22 | P2-51 |
| R-4 | Worktree/文件写入 TOCTOU (Windows 平台) | LOW | 接受 | 2026-10-22 | P2-54/55 |

---

## 2. 详细信息

### R-1: MicroCompactor 就地修改输入列表

| 属性 | 值 |
|------|-----|
| **ID** | R-1 |
| **文件** | [context/compaction.py:1022](../context/compaction.py#L1022) — `MicroCompactor.compact()` |
| **严重度** | LOW |
| **评级** | 接受 |
| **触发条件** | 调用方传入的 `messages` 列表是共享引用 (非 copy), MicroCompactor 修改 `messages[i] = {"content": "..."}` 污染调用方 |
| **当前缓解** | 所有已知调用方 (`agent/core.py`, `context/manager.py`) 在传递前调用 `history.to_dicts()` 创建副本。Phase 5 code review 确认无直接引用传递 |
| **升级路径** | 若有新调用方未走 `to_dicts()` 路径 → 在 MicroCompactor 内部添加 `copy.deepcopy` 防御性复制 |
| **触发模块变更时需重新评估** | `context/compaction.py`, `context/manager.py`, 任何新增调用 `MicroCompactor.compact()` 的模块 |
| **复审日期** | 2026-10-22 |

### R-2: Hook 异常静默吞没 (blockable 路径)

| 属性 | 值 |
|------|-----|
| **ID** | R-2 |
| **文件** | [hooks/dispatcher.py:81](../hooks/dispatcher.py#L81) — `_dispatch()` internal hook exception |
| **严重度** | LOW |
| **评级** | 接受 |
| **触发条件** | Internal hook (内存回调) 在 blockable event (PreToolUse) 期间抛出异常 → 当前仅记录 DEBUG 日志, 不阻断 |
| **当前缓解** | Internal hooks 为纯 Python 内存操作 (无 I/O, 无网络), 理论上无异常路径。PreToolUse hooks 返回 `BLOCK` 的 exit-code 逻辑在外部 hooks 中处理正确 |
| **升级路径** | 若有 internal hook 添加 I/O 操作 → 必须升级为 FAIL_CLOSED (异常时默认拒绝) |
| **触发模块变更时需重新评估** | `hooks/registry.py` (internal hook 注册), 任何新增 internal hook 的模块 |
| **复审日期** | 2026-10-22 |

### R-3: `_ROOT_REMOVAL_PATTERNS` 黑名单可绕过

| 属性 | 值 |
|------|-----|
| **ID** | R-3 |
| **文件** | [hitl/pipeline.py:696-715](../hitl/pipeline.py#L696-L715) — `_ROOT_REMOVAL_PATTERNS` |
| **严重度** | LOW |
| **评级** | 接受 |
| **触发条件** | Agent 处于 `bypassPermissions` 模式, 执行 `find / -delete`, `rm -rf --no-preserve-root /`, `chmod 000 -R /` 等未在黑名单中的等价破坏性命令 |
| **当前缓解** | `bypassPermissions` 模式仅由用户显式授权 (`--auto-approve` flag)。文档注释已说明黑名单仅为 "advisory guardrail", 非安全边界 |
| **升级路径** | 生产部署时添加文件系统级防护 (Docker volume read-only / bwrap sandbox / macOS Seatbelt 沙箱) |
| **触发模块变更时需重新评估** | `hitl/pipeline.py` (permission mode logic), 任何变更 bypassPermissions 行为的模块 |
| **复审日期** | 2026-10-22 |

### R-4: Worktree 写入 TOCTOU (Windows 平台)

| 属性 | 值 |
|------|-----|
| **ID** | R-4 |
| **文件** | [agent/session/worktree_manager.py:198-201](../agent/session/worktree_manager.py#L198-L201), [core/base.py:434-437](../core/base.py#L434-L437) |
| **严重度** | LOW |
| **评级** | 接受 |
| **触发条件** | Worktree `discard()`: `Path.resolve()` 跟随软链接, 若攻击者在 `resolve` 和后续操作之间替换目标 → 路径逃逸。`safe_open_for_write`: Windows 上 `p.is_symlink()` → `_os.open()` 为非原子操作 |
| **当前缓解** | Worktree 名称由 server-side 生成 (`definition_name + agent_id`), 非用户可控。Windows 软链接需管理员权限或 Developer Mode |
| **升级路径** | 部署到安全敏感环境时, 启用 Git worktree 的 `--lock` 选项或使用嵌套虚拟化 (Hyper-V / WSL2 容器) |
| **触发模块变更时需重新评估** | `agent/session/worktree_manager.py`, `core/base.py` (safe_open_for_write), 任何基于 Windows 的生产部署 |
| **复审日期** | 2026-10-22 |

---

## 3. 复审协议

### R-5: SessionTree 动态内联样式 (CSS 迁移例外)

| 属性 | 值 |
|------|-----|
| **ID** | R-5 |
| **文件** | [web/src/components/SessionTree.tsx](../web/src/components/SessionTree.tsx) — 3 处动态内联样式 |
| **严重度** | LOW |
| **评级** | 接受 (CSS 迁移 12/15 block，3 例外) |
| **触发条件** | `marginLeft: depth * 12` (递归深度计算), `color` (状态动态映射), `fontWeight: isActive ? 600 : 400` (活动状态) — 三个值均因运行时变量无法静态映射为纯 CSS class |
| **当前缓解** | 其他 12 处内联样式已迁移至 `styles.css: .session-tree-node-*`；CSS lint 脚本在计数时排除已记录的例外样式。Phase 7 Batch B 计划已引入 CSS lint 持续监控，若新增内部样式必须遵守迁移规范 |
| **升级路径** | 引入 CSS-in-JS 库或 CSS 变量 (`--session-depth`) 替代 `depth * 12` 作为可配置化预处理 |
| **复审日期** | 2026-10-22 |

### 季度复审议程 (每人 5 分钟)

1. 风险是否仍存在? (代码未变更, 缓解未废止)
2. 触发条件是否变化? (新调用方, 新部署模式)
3. 评级是否需要调整? (升级 / 降级 / 关闭)
4. 若关闭: 记录关闭原因 + 关闭日期

### 复审负责人

Solo-dev 模式: 由 guo 在每季度首周执行, 输出至 `docs/RISK_REGISTER.md` diff commit。

### 复审提醒机制

本文件 last-modified date 的季度间隔 > 90 天时, `tools/_quality_gate.sh` 发出警告 (非阻断)。

---

## 4. 风险登记册维护契约

- 新增风险: 任何 ASSESSED / ACCEPTED P2 必须在此登记, 禁用默认继承历史结论
- 关闭风险: 在条目中增加 `关闭日期` + `关闭原因`, 保留原文不可删除
- 评级变更: 在条目中增加 `变更日期` + `变更原因` + `新评级`

---

*首次复审: 2026-10-22, 由 guo 执行。*
