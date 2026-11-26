# Terminal-Bench 2.1 评测版本台账

## 第一批：GoalKeeper 2.2 原始版本

- 评测目录：`output/terminal-bench-2.1-pilot/pilot-20261014-f`
- 状态：已冻结，只读保留；后续修复和回归不得写入这个目录。
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- GoalKeeper 结果：5 题通过 4 题，`build-cython-ext` 未通过。

用于确认原始结果没有被覆盖的 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `manifest.json` | `6836f6c15372afb5083257163df478721a7932e87671eb83ebf07755389e52e9` |
| `results.json` | `e093f87bbcecdf5069167e7c985fd3e2cae48b0eb7dfe2b92cbf2d9ab2338fbb` |
| `results-final.json` | `bd0b09c5d39c2a0862249149a068e0ab92834cb80fabcc5fb34445588fb8e2ea` |
| `report.md` | `37e5e4db2c5f5828b28910d13812cce37bf081685b8da731bd90873a2419fdb3` |
| `dashboard.html` | `e31b161933287982c8d30cf976e5a6b76d4d875d9b98c5bc11c27afb498269f1` |

## 修复版：GoalKeeper 2.2.1

2.2.1 只调整依赖安装的时间控制和相应提示：

- 一次 `install_dependencies` 最多运行 180 秒，超时后把控制权交还给模型。
- 提示模型先安装当前步骤必需的最小依赖，并在超时后缩小依赖集合或更换方案。
- 普通 `bash` 命令继续使用任务请求的时限，避免误伤 Nginx 安装等耗时较长但仍在推进的操作。
- 评测脚本可以指定题目和评测组，回归结果写入新目录。
- 回归可以复用同一 Terminal-Bench 源码提交下已经通过的官方参考解，新的清单和参考解结果会明确记录复用来源。
- 官方参考解若超时会保存为未通过记录，评测程序不会因未处理的异常丢失本批运行记录。

2.2.1 的回归目录必须使用新的 campaign 名称。结果通过后才能开始第二批正式评测，且不能与 2.2 原始版本的第一批成绩混写。

## 2.2.1 回归门槛

1. `build-cython-ext`：单次依赖安装不再耗尽 900 秒，程序能够继续进入修改、编译和检查阶段。
2. `nginx-request-logging`：仍通过官方验收，证明长时间系统配置没有被统一截断。
3. `fix-git`：仍通过官方验收，且耗时和 Token 没有出现无法解释的明显增长。

## 2.2.1 回归结果

- 回归目录：`output/terminal-bench-2.1-pilot/regression-2.2.1-20261015-c`
- 回归说明：`output/terminal-bench-2.1-pilot/regression-2.2.1-20261015-c/comparison.md`
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- 官方参考解：复用 `pilot-20261014-f` 中同一源码提交下已经通过的结果；复用来源已写入本批 `manifest.json` 和 `oracle-results.json`。

| 题目 | 2.2 原始版本 | 2.2.1 | 判断 |
|---|---:|---:|---|
| `build-cython-ext` | 未通过；907.3 秒；25,375 Token | 通过；649.2 秒；854,132 Token | 完成率和耗时改善；Token 显著增加 |
| `nginx-request-logging` | 通过；897.2 秒；13,525 个已知 Token，计量不完整 | 通过；860.9 秒；33,249 Token，计量完整 | 长时间系统命令没有因修复退化；Token 不能直接同比 |
| `fix-git` | 通过；128.3 秒；102,539 Token | 通过；159.4 秒；147,319 Token | 仍通过，但耗时和 Token 增加 |

`build-cython-ext` 的运行记录显示，第一次完整依赖安装在 180.10 秒后以 `timed_out: true` 返回。程序随后继续修改源码、构建三个 Cython 扩展并通过官方验收。因此本次修复解决了“单次依赖安装耗尽全部任务时间”的直接问题。

这组结果不能表述为三个指标全面提升。2.2.1 提高了三题回归的完成率并缩短了目标失败题的耗时，但 Token 成本明显上升；第二批正式评测开始前，应先决定是否继续优化上下文和调用次数。

## 修复版：GoalKeeper 2.2.2

2.2.2 针对第一批正式评测中 `nginx-request-logging` 暴露出的系统依赖安装问题进行修复：

- 在支持 apt 的隔离环境中提供独立的系统依赖安装能力，模型不再通过普通 `bash` 长时间执行 `apt-get`。
- 安装程序只安装当前步骤需要的包，并为后续配置、启动服务和验收预留至少 120 秒。
- 安装过程按输出和 apt 缓存大小判断是否仍有进展；连续 90 秒没有进展时主动终止，把控制权交还给模型。
- 相同安装操作若没有产生进展，不允许原样反复执行。
- 普通命令同样不能占满任务全部剩余时间。
- 这项能力只在评测环境明确声明使用 apt 时出现；桌面任务和其他运行环境不会看到无法执行的系统安装入口。

### 2.2.2 单题真实回归

- 回归目录：`output/terminal-bench-2.1-pilot/regression-2.2.2-nginx-20261017-a`
- 题目：`nginx-request-logging`
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- 官方参考解：复用 `batch1-2.2.1-native-20261016-b` 中同一 Terminal-Bench 源码提交下已通过的结果。
- 结果：通过，官方奖励 `1`，官方测试 8 项全部通过。
- 总耗时：140.9 秒。
- 已知 Token：80,276；11 次模型调用；用量记录完整。
- 任务结束时仍剩余 765.5 秒，说明程序没有再把 900 秒全部耗在依赖下载上。

本次回归证明修复后的程序能够完成 Nginx 安装、配置、启动、请求验证和官方验收。该结果是 2.2.2 的独立回归证据，不覆盖 GoalKeeper 2.2 原始版本、2.2.1 第一批或第二批的任何成绩。

### 2.2.2 第一批完整对比结果

- 对比目录：`output/terminal-bench-2.1-pilot/batch1-2.2.2-comparison-20261017-a`
- GoalKeeper 其余四题：`output/terminal-bench-2.1-pilot/batch1-2.2.2-remaining-20261017-a`
- GoalKeeper Nginx 结果：复用上面的 2.2.2 单题真实回归。
- 三组对照：复用 `output/terminal-bench-2.1-pilot/batch1-2.2.1-native-20261016-b`，不重新运行，也没有关闭或更改各 Harness 自带的功能。
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`

| 评测对象 | 完成率 | 总耗时 | 已确认 Token | 用量记录 |
|---|---:|---:|---:|---|
| GoalKeeper 2.2.2（仅主 Agent） | 5/5 | 22分53秒 | 971,832 | 完整 |
| 评测题包 Harness（默认探索模式） | 3/5 | 34分10秒 | ≥1,054,839 | 1 次不完整 |
| Codex CLI（原生默认能力） | 4/5 | 31分54秒 | 1,285,176 | 完整 |
| Pi Agent（原生默认能力） | 4/5 | 27分03秒 | ≥538,996 | 1 次不完整 |

GoalKeeper 2.2.2 是本批唯一五题全部通过的组，总耗时也最短。GoalKeeper 与 Codex CLI 的用量记录均完整，GoalKeeper 的总 Token 比 Codex CLI 少约 24.4%。题包 Harness 和 Pi Agent 的总 Token 是下限，不能用于判断 GoalKeeper 是否比这两组更节省 Token。

`build-cython-ext` 虽然已经通过，但仍耗时 14分50秒并使用 617,785 Token，是 GoalKeeper 2.2.2 在本批中的主要效率短板。Nginx 结果来自独立回归批次，其余四题来自补跑批次；三组对照来自原生能力批次。五题的题目输入和官方判分均已核对有效，合并过程没有改写任何来源结果。

### 2.2.2 第二批逐题官方时限对比结果

- 对比目录：`output/terminal-bench-2.1-pilot/batch2-2.2.2-native-official-20261017-d`
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- 判分：每道题自带的官方验收程序。
- 时限：读取每道题 `task.toml`；`modernize-scientific-stack` 的 Agent 和 Verifier 均为 600 秒，其余四题均为 900 秒。
- 原生能力审计：20 次正式运行全部通过，未发现关闭或覆盖题包 Harness、Codex CLI、Pi Agent 自带功能的参数。

| 评测对象 | 完成率 | 五题总耗时 | 已确认 Token | 用量记录 |
|---|---:|---:|---:|---|
| GoalKeeper 2.2.2（仅主 Agent） | 3/5 | 9分45.6秒 | 318,795 | 完整 |
| 评测题包 Harness（默认探索模式） | 5/5 | 8分25.0秒 | 287,963 | 完整 |
| Codex CLI（原生默认能力） | 4/5 | 17分15.7秒 | ≥1,029,102 | 1 次不完整 |
| Pi Agent（原生默认能力） | 4/5 | 9分07.3秒 | 340,165 | 完整 |

本批中，评测题包 Harness 五题全部通过，同时总耗时最短、已确认 Token 最少。GoalKeeper 2.2.2 未通过 `sqlite-with-gcov` 和 `configure-git-webserver`：任务生成正常的构建或软件包符号链接后，GoalKeeper 的工作区版本计算把这些链接误判成交付文件，程序提前停止。`pypi-server` 也触发了相同错误，但服务在停止前已经启动，因此仍通过官方验收。以上结果按 2.2.2 的实际行为保留，没有修复后重跑。

Codex CLI 未通过 `pypi-server`，因为验收时本地 PyPI 服务不可访问；Pi Agent 未通过 `db-wal-recovery`，因为没有应用 WAL 中 `id=1` 从 100 更新为 150 的事务。Codex CLI 的 Token 仅能显示已确认下限，不能做精确成本排序。

原计划中的 `qemu-startup` 没有计分。其官方参考解在当前 Apple Silicon 主机的 amd64 Rosetta 容器中也因不支持的系统调用失败，第二批改用同一 Terminal-Bench 2.1 题库中的 `db-wal-recovery`。五道正式题的官方参考解均在正式环境通过。

## 桌面应用交付

- 程序坞当前目标：`outputs/unified-runtime-20261011/final-dist/mac-arm64/GoalKeeper.app`
- 桌面版本：2.2.2
- Harness 版本：2.2.2
- 原应用归档：`outputs/unified-runtime-20261011/archive/GoalKeeper-2.2-original.app`
- 2.2.1 应用归档：`outputs/unified-runtime-20261017-2.2.2/archive/GoalKeeper-2.2.1.app`
- 2.2.2 独立发布包：`outputs/unified-runtime-20261017-2.2.2/final-dist/mac-arm64/GoalKeeper.app`
- 2.2.1 部署记录：`outputs/unified-runtime-20261011/deployment-2.2.1.json`
- 2.2.2 部署记录：`outputs/unified-runtime-20261017-2.2.2/deployment-2.2.2.json`
- 包内架构检查：通过；唯一执行类为 `goalkeeper.engine.Engine`。
- 启动检查：通过；程序坞路径下的 Electron 主进程和 Python 后端进程均已启动。

## GoalKeeper 2.2.4 第三批逐题官方时限对比结果

- 对比目录：`output/terminal-bench-2.1-pilot/batch3-2.2.4-native-official-20261017-d`
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- 判分和时限：逐题读取 Terminal-Bench 2.1 官方 `task.toml`，并运行每题自带的验收程序。
- 原生能力审计：20 次正式运行全部通过，未发现关闭或覆盖题包 Harness、Codex CLI、Pi Agent 自带功能的参数。
- 用量：20 次正式运行均取得完整 Token 记录。

| 评测对象 | 完成率 | 五题总耗时 | Token | 模型调用 |
|---|---:|---:|---:|---:|
| GoalKeeper 2.2.4（仅主 Agent） | 4/5 | 18分56.6秒 | 412,601 | 54 |
| 评测题包 Harness（默认探索模式） | 4/5 | 17分41.6秒 | 324,939 | 46 |
| Codex CLI（原生默认能力） | 3/5 | 16分22.1秒 | 668,397 | 50 |
| Pi Agent（原生默认能力） | 4/5 | 16分45.5秒 | 318,300 | 51 |

GoalKeeper、题包 Harness 和 Pi Agent 都通过 4/5，Codex CLI 通过 3/5。在完成率同为 4/5 的三组中，Pi Agent 的总耗时和 Token 最少；GoalKeeper 本批没有取得耗时或 Token 优势。GoalKeeper 相比 Codex CLI 多通过一道题，并少消耗 255,796 Token。

四组都未通过 `dna-assembly`。它们都生成了要求的引物文件，但至少一对引物的正向、反向熔解温度差超过官方上限 5°C。Codex CLI 另外未通过 `query-optimize`：结果正确性相关检查通过，但生成查询的中位运行时间慢于官方基准，没有达到性能门槛。

原计划的 `financial-document-processor` 连续两次在官方参考解阶段达到 1200 秒上限，第二次已经命中镜像和公开软件包下载缓存。它没有计入任何组，替换为同一固定提交中官方参考解可通过的 `sqlite-db-truncate`。其余正式题为 `large-scale-text-editing`、`regex-log`、`dna-assembly` 和 `query-optimize`。

## GoalKeeper 2.2.4 第四批逐题官方时限对比结果

- 对比目录：`output/terminal-bench-2.1-pilot/batch4-2.2.4-native-official-20261018-c`
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- 判分和时限：逐题读取 Terminal-Bench 2.1 官方 `task.toml`，并运行每题自带的验收程序。
- 原生能力审计：20 次正式运行全部通过，未发现关闭或覆盖题包 Harness、Codex CLI、Pi Agent 自带功能的参数。
- 用量：20 次正式运行均取得完整 Token 记录。

| 评测对象 | 完成率 | 五题总耗时 | Token | 模型调用 |
|---|---:|---:|---:|---:|
| GoalKeeper 2.2.4（仅主 Agent） | 5/5 | 12分02.7秒 | 777,809 | 72 |
| 评测题包 Harness（默认探索模式） | 5/5 | 9分00.9秒 | 615,692 | 46 |
| Codex CLI 0.159.0（原生默认能力） | 4/5 | 11分54.7秒 | 1,223,609 | 58 |
| Pi Agent（原生默认能力） | 5/5 | 12分21.4秒 | 572,712 | 48 |

GoalKeeper、题包 Harness 和 Pi Agent 都通过 5/5，Codex CLI 通过 4/5。题包 Harness 在完成率同为 5/5 的三组中总耗时最短，Pi Agent 的 Token 最少。GoalKeeper 比 Pi Agent 快约 19 秒，但多消耗 205,097 Token；相比题包 Harness，GoalKeeper 更慢且 Token 更多。本批没有形成 GoalKeeper 在完成率、耗时和 Token 三项指标上的全面优势。

Codex CLI 只在 `kv-store-grpc` 未通过。它完成了服务代码、Protocol Buffers 文件和生成代码，执行期间的 Set/Get RPC 也成功；但服务通过前台工具调用启动，没有保留为独立后台进程。Agent 结束后进程随工具会话停止，官方验收时 5328 端口未监听。

五道题的官方参考解均通过。`fix-code-vulnerability` 的官方验收脚本会自行安装 pytest；预检阶段修正了评测程序的 Python 路径，使依赖安装和验收使用同一解释器。官方题目、验收脚本和评分标准均未修改，四组正式运行使用同一修正后的验收环境。
