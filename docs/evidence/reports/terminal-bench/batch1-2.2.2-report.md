# Terminal-Bench 2.1 第一批：GoalKeeper 2.2.2 与三组原生 Harness 对比

> 模型统一为 `gpt-5.6-sol`，推理强度统一为 `medium`，使用本机已登录的 Codex/ChatGPT 订阅账户。

## 汇总结果

| 评测对象 | 完成率 | 总耗时 | 平均每题耗时 | 已确认 Token | 平均每题 Token | 用量完整 |
|---|---:|---:|---:|---:|---:|:---:|
| GoalKeeper 2.2.2（仅主 Agent） | 5/5（100%） | 22分53秒 | 4分35秒 | 971,832 | 194,366 | 是 |
| 评测题包 Harness（默认探索模式） | 3/5（60%） | 34分10秒 | 6分50秒 | ≥1,054,839 | ≥210,968 | 否 |
| Codex CLI（原生默认能力） | 4/5（80%） | 31分54秒 | 6分23秒 | 1,285,176 | 257,035 | 是 |
| Pi Agent（原生默认能力） | 4/5（80%） | 27分03秒 | 5分25秒 | ≥538,996 | ≥107,799 | 否 |

## 逐题结果

| 题目 | GoalKeeper 2.2.2 | 题包 Harness | Codex CLI | Pi Agent |
|---|---|---|---|---|
| `fix-git`<br>修复 Git 仓库状态 | 通过；2分25秒；190,468 Token | 通过；1分25秒；70,044 Token | 通过；2分00秒；172,501 Token | 通过；1分02秒；32,673 Token |
| `build-cython-ext`<br>修复并编译 Cython 扩展 | 通过；14分50秒；617,785 Token | 未通过；15分09秒；≥924,577 Token | 未通过；15分27秒；680,964 Token | 未通过；15分05秒；≥436,019 Token |
| `multi-source-data-merger`<br>合并多种格式的数据 | 通过；1分48秒；53,915 Token | 通过；1分18秒；20,799 Token | 通过；1分13秒；33,493 Token | 通过；0分59秒；11,647 Token |
| `nginx-request-logging`<br>安装并配置 Nginx 请求日志 | 通过；2分21秒；80,276 Token | 未通过；15分11秒；28,785 Token | 通过；11分30秒；346,513 Token | 通过；8分27秒；44,185 Token |
| `cancel-async-tasks`<br>修复异步任务取消和资源清理 | 通过；1分29秒；29,388 Token | 通过；1分07秒；10,634 Token | 通过；1分44秒；51,705 Token | 通过；1分30秒；14,472 Token |

## 可以直接得出的结论

- GoalKeeper 2.2.2 在这五题中通过 5/5，是唯一全部通过的组。Codex CLI 与 Pi Agent 各通过 4/5，题包 Harness 通过 3/5。
- GoalKeeper 2.2.2 的五题总耗时为 22分53秒，在本批四组中最短；但 `build-cython-ext` 单题耗时 14分50秒，几乎用满 900 秒，仍是明显的效率短板。
- GoalKeeper 2.2.2 的用量记录完整，共 971,832 Token。Codex CLI 的用量也完整，共 1,285,176 Token；本批 GoalKeeper 比 Codex CLI 少约 24.4%。
- 题包 Harness 与 Pi Agent 都有一次运行未返回完整用量，因此它们的 Token 只能写成下限，不能据此得出 GoalKeeper 一定比它们更省 Token。
- GoalKeeper 2.2.2 的 `nginx-request-logging` 来自独立回归批次，其余四题来自补跑批次；三组对照来自前一天完成的原生能力批次。完成率可以直接比较，耗时和 Token 还会受到模型波动、网络和缓存状态影响。

## 数据来源与公平性检查

- 三组对照来自 `batch1-2.2.1-native-20261016-b`，其 `native-capabilities-audit.json` 显示 20/20 次运行均保留各 Harness 的原生默认功能，没有关闭子 Agent、记忆、联网检索或扩展能力。
- GoalKeeper 2.2.2 仍只有主 Agent，没有新增子 Agent。
- 五题均使用同一 Terminal-Bench 2.1 源码提交、同一题目内容和同一官方验收脚本；所有记录均通过题目输入核对并具有有效官方判分。
- 本目录只保存合并结果和报告，没有改写三个来源批次的原始结果。
