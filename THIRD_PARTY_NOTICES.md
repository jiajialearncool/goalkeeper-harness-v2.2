# 第三方来源与许可证状态

## 本项目的许可选择

当前来源目录未发现项目自身的 LICENSE 文件。本地候选尚未代项目所有者选择或授予开源许可证。公开前需要确认本项目许可和署名方式；当前文件不构成第三方代码的许可声明。

## 依赖与参考材料

Python 依赖通过 [requirements.txt](requirements.txt) 安装，包名和固定版本来自留存后端；未将第三方包源码复制到仓库。安装环境中各包的声明见 [依赖许可证清单](docs/evidence/dependency-licenses.json)，声明未自动等同于完整法律审查。

Codex、Pi、OpenCode 只作为锁定版本的设计比较和评测对象，仓库提供官方源码链接及少量机制说明，未复制完整源码。定稿图是项目说明材料，不代表这些产品的官方架构图。

Terminal-Bench 题目和评分器未重新分发。仓库发布本地运行的汇总结果，并标明上游题目提交；完整任务材料需按上游的许可和获取方式准备。原始私有题目、数据、隐藏评分器和答案不公开。

## 桌面前端

留存应用包含基于 DeepSeek Harness 前端的编译产物。当前公开候选未复制 desktop/vendor 或整份前端编译包。后端桌面连接代码保留在 src/goalkeeper/desktop；桌面源码来源、准确版本、许可证与构建过程补齐后，可另行准备桌面分发版本。

## 展示方式参考

README 结构参考了 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)、[Deep Agents](https://github.com/langchain-ai/deepagents)、[Pi](https://github.com/earendil-works/pi) 和 [Harbor](https://github.com/harbor-framework/harbor) 的官方说明。仅借鉴信息组织方式，未据此声称 GoalKeeper 具备这些项目的全部功能。
