# 安装、运行与复现范围

[返回首页](../README.md)

## 环境与依赖

命令沙箱依赖 macOS Seatbelt，Python 使用 3.11 或 3.12。本次实际验证解释器为 3.12.14。Linux 和 Windows 未在这份源码中提供等价的无沙箱运行保证，不应为了跑示例绕过权限检查。

下载仓库后，在仓库根目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
PYTHONPATH=src .venv/bin/python -m goalkeeper.cli --help
```

requirements.txt 来自留存后端，版本和哈希保持原样。uv.lock 从同一留存应用补回，已与应用发布清单校验；本次新增 pyproject.toml 便于查看包元数据。安装以带哈希的 requirements.txt 为准，未把新元数据对应的 uv sync --locked 作为已验证入口。

本版按源码工作目录运行。运行路径、配置和任务输出均位于仓库内，需保证仓库可写。不要把只读安装目录中的 wheel 当作已支持的桌面分发方案。

## 先运行不调用模型的演示

```bash
PYTHONPATH=src .venv/bin/python examples/prepare_demo.py
PYTHONPATH=src .venv/bin/python examples/offline_demo.py
```

第一条命令准备模拟销售表及自然语言任务 JSON，不运行 Agent。第二条命令把固定模型返回交给原版执行引擎，真实执行文件读写和交付核对。首次初始化 tiktoken 分词器时，可能需要下载公开词表；词表不包含用户数据或模型权重。

预期过程：

1. 读取 sales.csv；
2. 模型固定返回申请交付，但没有 totals.csv，框架拒绝；
3. 实际文件工具写入 totals.csv；
4. 第二次申请被接受，交付完成，保存只读快照。

输出目录为 work/offline-demo/ 下的新编号目录，不覆盖旧记录。示例断言检查申请先拒绝、后接受，以及快照内容和只读属性。没有运行真实模型，也没有运行独立业务评分；verification=unverified 表示未提供可执行检查证据，不应解释成已经通过完整业务验收。

## 使用真实模型运行自然语言任务

本次发布准备未执行以下模型请求。运行者需使用自己的账户、确认接口和模型可用性，并承担实际使用产生的费用。

```bash
PYTHONPATH=src .venv/bin/python -m goalkeeper.cli login --port 8892
```

根据终端提示完成本机授权后，在另一个终端运行：

```bash
PYTHONPATH=src .venv/bin/python -m goalkeeper.cli run \
  --task "$PWD/examples/generated/task.json" \
  --source "$PWD/examples/generated/initial" \
  --config "$PWD/configs/default.json" \
  --allow-real-model
```

默认配置里的模型为 gpt-6.1-sol，effort=auto。可用模型与推理强度同时受资料包内 provider_catalog 和账户能力限制，不能只修改字符串就保证任意新模型可用。API Key 与不同提供方的配置见源码 runtime/providers.py；这里仅展示原有账户入口，未声称个人订阅自动具备通用 API 权限。

命令输出实际运行编号。将下面的 RUN_ID 换为该编号，查看状态或从允许的中断状态继续：

```bash
PYTHONPATH=src .venv/bin/python -m goalkeeper.cli report --run "$PWD/runtime/runs/RUN_ID"
PYTHONPATH=src .venv/bin/python -m goalkeeper.cli resume --run "$PWD/runtime/runs/RUN_ID" --allow-real-model
```

resume 受原有状态、剩余时间和恢复次数限制，不等于重新开始一次评测。原始 CLI 不提供桌面聊天界面；普通自然语言任务不要求用户编写 checks，示例准备脚本负责封装任务输入。

## 离线回归与公开文件检查

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m pytest tests -q
.venv/bin/python scripts/verify_publication.py
```

tests/test_unified_runtime.py 是从开发工作树取得的兼容测试副本；新增 test_publication_runtime.py 用真实文件、固定模型与部分真实本机检查命令验证核心行为。来源与校验值单列，不冒称资料包包含完整历史测试套件。测试不会请求真实模型。

## 历史评测的复现边界

| 材料 / 行为 | 本仓库可以做什么 | 还缺什么 |
|---|---|---|
| 历史16题公开数据 | 复算请求、耗时、已知用量和匹配子集比较 | 完整初始题包、部分原始隐藏评分器、逐次冻结实现和运行环境 |
| Terminal-Bench各批报告 | 核对题目、时限、官方通过/失败、分批成本 | 完整历史适配器、冻结启动清单和原始运行数据未全部公开 |
| 2.2.4运行逻辑 | 阅读全部后端源码，运行自然语言最小任务与离线回归 | 真实模型与账户接入需在运行者环境另行验证 |
| 桌面应用 | 阅读原版后端连接源码 | 完整对应前端原始工程、构建依赖和桌面分发材料尚未补齐 |

发布文件没有带入私人凭据、运行数据库、旧答案、后台记忆或完整任务数据集。可复算历史统计与可重新执行历史实验的范围已经分开说明。
