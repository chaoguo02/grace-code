# Grace Code 面试演示指南

这份指南与 Web `Overview` 页面使用同一条叙事主线。目标不是罗列功能，而是用持久化证据回答三个问题：

1. Agent 如何完成一次可解释的开发任务？
2. 多 Agent 并发时如何处理上下文、权限和工作区一致性？
3. 系统如何证明失败没有被隐藏，并能被复盘和恢复？

## 演示前检查

打开 Web 的 `Overview` 页面，先检查证据状态：

- `Observed evidence`：当前项目存在可展示的运行证据。
- `Configured`：能力已经接入，但当前窗口没有运行样本。
- `Evidence unavailable`：需要先选择对应 session，或当前能力未启用。

演示前建议准备：

- 一条成功完成、包含工具调用和文件修改的 session。
- 一条包含子 Agent 的 session；如果涉及写操作，保留 worktree 处置记录。
- 一条失败、取消或预算耗尽的 run。
- 至少一份 Evaluation Lab 能读取的 validation artifact。

缺少某项时不要临时伪造数据。直接说明“能力已配置，但这次没有可验证样本”，然后展示接口的 disclosure。

## Demo 1：正常开发闭环

建议时长：6 分钟。

### 讲述目标

Grace Code 不只生成最终文本；一次开发任务会留下运行、工具、上下文、验证和工作区证据。

### 操作顺序

1. 在 `Chat` 新建或打开一条开发 session。
2. 提交一个范围明确、可验证的修改任务。
3. 执行结束后进入 `Runs`：
   - 指出 typed terminal outcome；
   - 展示 steps、tokens、verification 和 workspace delta；
   - 说明最终回答与运行状态是不同的数据边界。
4. 进入 `Context`：
   - 展示实际 provider request 的 token 组成；
   - 展示 tool/skill/MCP capability surface；
   - 如果发生压缩，说明压缩发生在哪个请求边界。
5. 进入 `Review`：
   - 展示 diff 和人工决策；
   - 强调 Agent 完成不等于变更自动被接受。

### 推荐讲法

> 我们把“Agent 说完成了”和“系统有证据证明它完成了”分开。回答属于交互层，run、verification 和 workspace delta 属于事实层。

## Demo 2：子 Agent 与 worktree 一致性

建议时长：7 分钟。

### 讲述目标

多 Agent 不是把多个提示词同时发送出去。系统必须明确任务归属、上下文来源、调度位置、完成回执和代码收敛方式。

### 操作顺序

1. 选择一条包含子 Agent 的 session，进入 `Agents`。
2. 在 delegation tree 中展示：
   - parent / child 关系；
   - foreground / background placement；
   - generation 和运行状态。
3. 选择不同节点，对比：
   - `fresh`、`parent_snapshot`、`resumed`；
   - `own_history` 与 `snapshot_copy`；
   - 当前工作区或隔离 worktree。
4. 查看 completion receipt：
   - pending 表示结果尚未被父 Agent 认领；
   - delivered 表示已被父 Agent 原子消费；
   - 打开页面本身不会改变投递状态。
5. 如果存在 worktree，展示 preserved / applied / discarded / retained：
   - preserved 不能被静默当作已经合并；
   - apply/discard/retain 必须形成显式收敛决策。
6. 进入 `Safety`，说明子 Agent 不能放宽父 Agent 的 deny 和权限边界。

### 推荐讲法

> 子 Agent 与 Skill 的区别是：子 Agent 有独立 session、生命周期、上下文和结果回执；Skill 是按需加载的指令与能力修饰，不拥有独立调度身份。

## Demo 3：失败、取消与恢复

建议时长：6 分钟。

### 讲述目标

失败不会被包装成普通回答，也不会在刷新后消失。终止原因、事件顺序和恢复代际都有持久化边界。

### 操作顺序

1. 选择失败、取消或预算耗尽的 session，进入 `Replay`。
2. 指出 replay contract 的来源：
   - `persisted_replay_run` 是完整运行时契约；
   - reconstructed 或 legacy 数据会明确标注证据限制。
3. 查看 termination status、termination reason 和 step boundary。
4. 进入 `Trace`，展示有序事件以及 terminal event。
5. 进入 `Health`：
   - 展示失败分类；
   - 展示成功率、P95、tool error rate；
   - 说明 reference objective 不是生产 SLA。
6. 回到 `Chat` 执行显式 resume/retry：
   - 新运行使用新的 run identity；
   - resumed 子 Agent 增加 generation；
   - 历史失败仍然保留。

### 推荐讲法

> 可恢复性的前提是失败首先可见。我们不会用新的成功回答覆盖旧失败，而是保留旧 run，再创建新的运行代际。

## 一页式架构讲述

进入 `System` 页面，按以下顺序说明：

1. Interface：Chat、Plan、HITL、Review。
2. Orchestration：SessionRuntime、Agent registry、subagent scheduler。
3. Reasoning：Context manager 和模型请求边界。
4. Capability：Tool、Skill、MCP。
5. Safety：Permission pipeline、hooks、path sandbox、human decision。
6. Persistence：session、run、trace、replay、notification、worktree evidence。

随后回到 `Overview`，用 capability cards 说明每个架构主张对应哪一个证据页面。

## 面试中的边界说明

- 普通 chat 完成不等于 evaluation pass。
- Token 数量不等于货币成本；没有版本化价格表时不展示金额。
- 当前 Agent 通信是委派与 direct-child completion receipt，不是任意 Agent 间消息总线。
- Peak parallelism 是从 session 时间区间重建的观测值，不是调度器模拟结果。
- 历史数据缺少新契约时会标注 reconstructed、legacy 或 unavailable。
- Overview 是只读聚合层；打开它不会认领通知、审批变更或执行工具。

## 演示结束语

> Grace Code 的重点不是页面数量，而是同一次运行能从交互、执行、上下文、安全、多 Agent、回放和质量七个角度得到一致的持久化解释。系统不知道的内容会显示为缺少证据，而不是自动推断成成功。
