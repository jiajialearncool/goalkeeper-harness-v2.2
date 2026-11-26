# 框架原理与源码入口

[返回首页](../README.md)

## 先分清三种职责

模型阅读本轮材料，生成下一步工具请求或答案；工具负责实际读写文件、运行命令；执行框架决定哪些工具可以执行、保存什么记录，以及是否接受交付。Agent 在本文指持续处理任务的模型角色。2.2.4 只有一个主 Agent；压缩摘要使用模型请求，不构成另一个独立接单的子 Agent。

框架直接通过模型接入模块发送请求，未把 GoalKeeper 主任务交给 Codex CLI 代跑。评测模块运行 Codex 等原生产品，是对照实验的另一条路径。[ResponsesProvider.stream](../src/goalkeeper/provider.py) · [create_engine](../src/goalkeeper/runtime/factory.py) · [评测模块](../src/goalkeeper/evaluation.py)

## 四步与十个环节

四步分别包含初始化①②③、主循环④、工具与压缩⑤⑥⑦、交付⑧⑨⑩。[定稿图1](figures/figure-1.mmd) 中的归档压缩是条件分支；状态保存会在执行过程中发生，末尾节点表示最终保留的记录，不表示此前没有保存。

### 第一步：建立任务、限定范围（①②③）

桌面用户选择工作目录并发消息，桌面连接模块把消息交给共用运行接口。`Runtime.new_run` 创建 `Task(delivery_mode='interactive')`，保存目标和文件基线，再调用共用创建接口启动唯一 `Engine`。

命令行原有入口读取任务 JSON；公开示例会帮助准备 JSON，用户不用手写自然语言验收标准和检查命令。各入口共享执行骨架，不等于界面和命令行的输入体验完全一致。

文件可写范围由 `Task.write_paths` 与 `Scope.path` 检查；命令在 macOS Seatbelt 沙箱中运行。2.2.4 默认时间上限为 1800 秒，即 30 分钟，来自 `RunConfig.seconds_budget` 与默认配置；任务可调整。桌面等待用户回复、继续任务等时间行为以共用运行接口为准，不能简单理解为所有对话总共只有半小时。

来源：[Runtime.new_run](../src/goalkeeper/runtime/session.py#L231)、[Manager.send](../src/goalkeeper/desktop/manager.py)、[create_engine](../src/goalkeeper/runtime/factory.py#L10)、[Scope.path](../src/goalkeeper/sandbox.py#L30)、[默认配置](../configs/default.json)。

### 第二步：让模型决定下一步，接住新要求（④）

每轮输入由任务事实、运行指令、摘要（存在时）以及近期消息与工具结果组成，同时提供工具定义。框架把模型提出的工具请求变成待执行操作，把真实结果再交回模型。用户中途增加的要求进入消息记录，`add_requirements` 把能识别的要求另存到 `state.requirements`；摘要压缩不会直接删除这份清单。

例如“再生成 report.pdf”会留下文件要求；如果用户明确改成其他格式，`inherit` 中的规则尝试识别替换和取消。文字规则有边界，不能保证理解任意复杂的需求变更。

来源：[Engine.before_turn / Engine.run](../src/goalkeeper/engine.py)、[facts / build](../src/goalkeeper/context.py#L51)、[add_requirements / inherit](../src/goalkeeper/runtime/completion.py#L81)、[运行提示](../src/goalkeeper/runtime/prompts.py)。

### 第三步：执行工具，历史太长就压缩（⑤⑥⑦）

框架先校验工具参数与文件范围，再执行操作。`execute_steps` 可以把顺序已确定的 2–8 步一次提交，遇到工具错误、超时或非零退出码便停止后续步骤；之前已写入的内容不会自动撤销。`success_condition` 在这份源码中为说明文字，不能解释成自动执行任意业务断言。

历史太长时，`compact` 先将较早片段原样写入 `archive/`，再请模型按七项生成结构化摘要：目标、约束、已完成、决定、未解决、下一步、来源。摘要通过格式、长度和来源检查后，框架带摘要与近期记录继续。完整工具调用及结果按事务切分，避免只留下调用、丢掉结果。

这里的白盒指开发者可以打开运行目录，核对 state.json、archive/ 中的原文以及压缩事件。模型内部思考不可见；桌面端没有压缩过程可视化界面。

| 2.2.4 配置字段 | 默认值 | 对实际运行的含义 |
|---|---:|---|
| compact_trigger | 48000 Token | 达到该值后尝试压缩，实际条件还会计入上下文估计 |
| context_ceiling | 64000 Token | 框架配置的本轮输入控制上限，不代表所有模型的最大窗口 |
| keep_recent_tokens | 20000 Token | 通用路径保留近期记录的目标；按完整事务切分，非严格裁成等长 |
| summary_max_tokens | 2000 Token | 对摘要的校验上限；运行提示还要求正文尽量压到约 800 Token |
| summary_input_tokens | 40000 Token | 摘要请求的输入控制参数 |
| tool_result_tokens | 4096 Token | 工具结果预览的长度控制，原始结果另有归档 |
| seconds_budget | 1800 秒 | 默认时间上限 |

研究报告任务在 `runtime/research.compact` 中采用 8000 Token 的近期保留目标，不能把配置里的 20000 写成所有任务统一值。摘要校验失败有一次纠正机会；仍失败会停止继续压缩、保留原历史，后续运行仍受输入与时间限制。

**读回分两层。** `search_archive/read_archive` 只处理 `material_refs` 中已登记、已读取的网页或文档原文，关键词匹配需要模型主动发起；被压缩的旧对话也保存在归档里，但不在这条模型工具读回范围内。开发者能打开核对，模型不能保证自动找回。语义检索未作为本版已实现能力。

来源：[Engine.execute_steps](../src/goalkeeper/engine.py#L542)、[compact](../src/goalkeeper/runtime/research.py#L57)、[SUMMARY_FIELDS / split_for_compaction](../src/goalkeeper/context.py)、[Store.archive / Store.read](../src/goalkeeper/storage.py)、[RuntimeTools.execute_uncached](../src/goalkeeper/runtime/capabilities.py#L259)、[配置](../configs/default.json)。

### 第四步：核对申请，保存交付（⑧⑨⑩）

模型调用 `request_delivery` 提交答案、文件、有效检查编号和未完成事项。框架检查申请字段，核对真实文件与检查记录，保存只读交付快照及最终状态。纯文字说“已经完成”不会直接代替交付申请。

两种模式共用 `Task` 和 `Engine.delivery`。普通自然语言任务的 `acceptance/checks` 可以为空；预设检查任务要求两个字段非空，检查编号不能重复。

来源：[Task.checks_unique](../src/goalkeeper/models.py#L110)、[Engine.delivery](../src/goalkeeper/engine.py#L353)、[review](../src/goalkeeper/runtime/completion.py#L228)、[Store.save](../src/goalkeeper/storage.py#L52)。

## 交付核对具体能保证什么

| 场景 | 检查从哪里来 | 框架实际核对 | 能力边界 |
|---|---|---|---|
| 自然语言 interactive | 用户原话经 derive 的文字匹配规则提取；现有检查可由模型请求运行 | 输出类型、数量、指定文件、适用检查证据和答案是否存在 | 不会自动生成 assert 脚本，不理解全部语义质量 |
| 评测/标准化 checked | 评测或部署任务的人事先提供 acceptance 与 checks | 在沙箱中运行适用检查，核对退出状态及文件/任务版本，检查通过才交付 | 检查覆盖不到的业务问题仍可能漏过；独立评分另行执行 |

`review` 在模型提交逐项结果时检查编号覆盖、状态和证据来源，并逐项检查捕获到的要求。旧格式申请未必带逐项结果；框架仍执行本地核对，并在结果中标记 legacy。文字回答主要确认有答案，文件检查也不能自动证明计算或业务解释正确。

`interactive` 允许明确声明未完成事项的部分交付。申请被接受时，运行 `phase` 可以进入 delivered，同时 `delivery.completion.status` 为 incomplete。页面或分析应读取完成情况，不能只看到 phase 就宣称全部完成。本例的完整交付状态同时满足两个字段。

`execute_check` 可以运行项目已有测试并登记证据；检查证据绑定文件、目标版本和命令等条件，发生变化后旧证据可能失效。框架尚未内置“自动为任意目标生成并运行通用测试”的完整能力。`Task.active_definition` 中的 never promote checks 注释约束任务定义的运行视图，不能单凭注释证明所有检查来源。

## 配置与模块导航

配置是开发者修改 JSON 文件的工程参数。并非每个参数都在聊天界面提供开关。默认模型与推理强度要区分：资料包默认模型为 gpt-6.1-sol，effort 为 auto；历史 16 题评测固定 xhigh；Terminal-Bench 记录固定 gpt-5.6-sol / medium，由对应适配环境运行。

| 源码 | 负责什么 |
|---|---|
| [runtime/factory.py](../src/goalkeeper/runtime/factory.py) | 创建唯一 Engine、装配运行服务 |
| [engine.py](../src/goalkeeper/engine.py) | 模型与工具循环、追加要求、检查与交付 |
| [runtime/completion.py](../src/goalkeeper/runtime/completion.py) | 从原话识别要求、核对交付文件和有效记录 |
| [context.py](../src/goalkeeper/context.py) | 组装输入、完整事务切分、摘要格式 |
| [runtime/research.py](../src/goalkeeper/runtime/research.py) | 归档压缩、符合条件的只读并行 |
| [runtime/capabilities.py](../src/goalkeeper/runtime/capabilities.py) | 工具接入、已登记资料的检索读回 |
| [sandbox.py](../src/goalkeeper/sandbox.py) | 文件范围与 macOS 命令沙箱 |
| [storage.py](../src/goalkeeper/storage.py) | 状态原子保存、归档和快照哈希 |
| [provider.py](../src/goalkeeper/provider.py) | HTTP 模型请求与流式结果 |
| [desktop/manager.py](../src/goalkeeper/desktop/manager.py) | 桌面消息、目录和事件连接 |

本版确实存在 runtime/ 目录与 desktop/manager.py。源码中没有 desktop/models.py；普通任务创建应引用 runtime/session.py，不能继续使用早期稿件里的 DesktopTask 继承说法。
