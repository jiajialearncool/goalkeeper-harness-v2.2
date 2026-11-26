# 代表性成功与失败案例

[返回首页](../README.md)

## 成功：在工单工具里追加批量撤销

历史 L1-v1 要求在已有工具中完成旧数据迁移、批量操作、查询分页、报表和审计；第一阶段后，在同一会话追加撤销功能。任务同时检查新功能和旧行为，适合观察 Agent 能否在连续修改时保留约束。

gpt-6.1-sol / xhigh 的优化后历史选定轮次中，四组都通过 18/18 项独立质量检查。GK 使用 11 次请求、5分54秒、146933 Token；Codex 为 17 次、8分30秒、456969 Token。这里比较的是报告选定轮次，不代表 2.2.4 重跑，不证明某一项设计独立造成收益。

面试时可以先说明“多轮追加和旧功能保留”这个问题，再展示结果，再打开检查项。框架能接收后续要求与登记检查是实现事实；该轮成绩是历史样本结果，两类证据要分别展示。

来源：[历史报告](evidence/reports/historical-16/benchmark-report.md)、[逐题检查数据](evidence/reports/historical-16/benchmark-results.json)、[Engine.feedback / Engine.delivery](../src/goalkeeper/engine.py)。

## 失败：看见了文件，仍未满足专业约束

Terminal-Bench 2.1 第三批的 dna-assembly 要求生成符合限制的 PCR 引物。四组都生成了引物文件，官方验收仍全部未通过。报告记录 GK 某对引物熔解温度差为 5.866°C，超过题目上限 5°C。

这说明文件存在与任务质量达标需要分别判断。通用文件检查能发现“没交文件”，专业约束需要相应测试或人工复核。交付闸门不能替代覆盖完整的独立评分。

来源：[第三批报告](evidence/reports/terminal-bench/batch3-2.2.4-report.md)。

## 效率短板：通过也可能付出更高成本

Terminal-Bench 2.1 第四批的 polyglot-c-py 要求同一份文件能以 Python 与 C 运行。GK 通过官方验收，但耗时 4分16.1秒、253719 Token；Pi 为 1分13.0秒、20935 Token。框架在这道题没有效率优势，不能仅展示通过率。

来源：[第四批报告](evidence/reports/terminal-bench/batch4-2.2.4-report.md)。

## 失败：MIPS 解释器任务

Terminal-Bench 2.1 第五批中，GK 2.2.4 的 make-mips-interpreter 未通过，耗时 10分04.7秒、904158 Token；Codex 与 Pi 通过。现有公开汇总不足以逐条证明失败的根因，因此这里仅陈述官方验收结果。pathgate-rc1 是候选版，成绩与本版分列。

来源：[第五批报告](evidence/reports/terminal-bench/batch5-2.2.4-comparison-report.md)。

## 发布准备新增的离线演示

[offline_demo.py](../examples/offline_demo.py) 将固定模型返回交给原版 Engine，观察缺文件时申请被拒绝、文件生成后申请通过，并核对只读快照。演示数据完全合成，不使用旧答案或私人运行数据库。

固定返回只验证框架流程。这个示例的运行次数、耗时和 Token 不进入任何历史评测表。
