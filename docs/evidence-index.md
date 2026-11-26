# 结论与源码、报告的对应关系

[返回首页](../README.md)

| 结论 | 公开一手依据 | 适用边界 |
|---|---|---|
| 单主 Agent，唯一执行引擎 | [create_engine](../src/goalkeeper/runtime/factory.py#L10)、[verify_single_runtime](../src/goalkeeper/runtime/architecture.py#L9) | 摘要模型请求不计为子 Agent 编排 |
| 桌面自然语言任务 checks 可空 | [Runtime.new_run](../src/goalkeeper/runtime/session.py#L231)、[Task](../src/goalkeeper/models.py#L87) | 原有 CLI 接收 JSON；示例帮助准备输入 |
| 从原话机械识别要求 | [derive / add_requirements / inherit](../src/goalkeeper/runtime/completion.py#L48) | 正则规则，不保证语义完整理解 |
| 交付检查证据来自真实产物 | [review](../src/goalkeeper/runtime/completion.py#L228)、[Engine.delivery](../src/goalkeeper/engine.py#L353) | 文字语义及检查覆盖外的质量不能保证 |
| 交互任务可交回未完成结果 | [Engine.delivery](../src/goalkeeper/engine.py#L421) | 需同时读取 completion.status，不能只看 phase |
| 预设检查交付强制运行适用 checks | [Task.checks_unique](../src/goalkeeper/models.py#L110)、[Engine.check / delivery](../src/goalkeeper/engine.py#L183) | 检查由评测/部署任务的人提供 |
| 检查证据会随文件或目标变化失效 | [Engine.key / current / execute_check](../src/goalkeeper/engine.py#L164) | 执行程序的证据有效性规则 |
| 先归档，再生成并校验七项摘要 | [compact](../src/goalkeeper/runtime/research.py#L57)、[SUMMARY_FIELDS / validate_summary](../src/goalkeeper/context.py#L91)、[Store.archive](../src/goalkeeper/storage.py#L69) | 保留近期长度随任务类型变化 |
| 已登记资料可关键词检索，压缩旧对话未接通工具读回 | [RuntimeTools.execute_uncached](../src/goalkeeper/runtime/capabilities.py#L259) | search/read_archive 限 material_refs |
| execute_steps 一次2–8步，失败停止后续 | [Engine.execute_steps](../src/goalkeeper/engine.py#L542) | 已执行操作不自动回滚；条件文字非自动业务断言 |
| 文件访问与命令执行有范围限制 | [Scope.path / Seatbelt.profile](../src/goalkeeper/sandbox.py#L30) | 命令沙箱当前依赖 macOS |
| 独立 HTTP 请求模型 | [ResponsesProvider.stream](../src/goalkeeper/provider.py#L54) | 服务可用性与账户权限需另外确认 |
| 历史16题的正常交付、质量与未知用量 | [报告](evidence/reports/historical-16/benchmark-report.md)、[数据](evidence/reports/historical-16/benchmark-results.json) | 多个历史快照，非本版全量重测 |
| Terminal-Bench各批通过、失败与成本 | [分批说明](evaluation/terminal-bench.md)、[版本台账](evidence/reports/version/terminal-bench-2.1-version-ledger.md) | 第一批2.2.2；候选版另列；源码哈希部分缺失 |
| 定稿图原样保留 | [原始文件](figures/gpt_figures_final.mmd)、[拆分哈希](evidence/figures-manifest.json) | 图3/5预设检查模式说明已补 |
| 个人决策与AI实现分工 | [匿名摘录](contribution.md)、[来源哈希](evidence/decision-source-hashes.json) | 原始私人对话未公开 |
| 本次离线验证通过范围 | [验证说明](validation.md)、[机器结果](evidence/validation.json) | 固定返回测试不代表真实模型表现 |

竞品源码证据单列在[对比说明](comparison.md)，以具体版本、文件与函数追溯。无法确认的服务端内部算法、被压原文自动找回以及子 Agent 进程隔离，不用于支持差异结论。
