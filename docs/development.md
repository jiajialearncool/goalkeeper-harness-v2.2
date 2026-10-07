# 开发与四组评测

## 安装

从源码 checkout 使用 Python 3.11/3.12 和 uv sync --locked。macOS 是当前完整支持平台。核心 CLI 不需要原生对照产品；完整回归和四组比较需要 Node.js/npm 与固定对照依赖。

固定版本：Codex 0.159.0、Pi 0.73.1、OpenCode 1.18.34。Python 锁文件是 uv.lock，npm 锁文件是 configs/native-package-lock.json。

```bash
.venv/bin/python scripts/install_pinned_codex.py
.venv/bin/python - <<'PYCODE'
import json
from pathlib import Path
root = Path('work/native-tools')
root.mkdir(parents=True, exist_ok=True)
lock = Path('configs/native-package-lock.json').read_text()
(root / 'package-lock.json').write_text(lock)
dependencies = json.loads(lock)['packages']['']['dependencies']
(root / 'package.json').write_text(json.dumps({'name': 'native-tools', 'private': True, 'dependencies': dependencies}, indent=2))
PYCODE
npm ci --prefix work/native-tools --ignore-scripts --cache work/install-cache/npm
# 核对固定 opencode-ai 的安装脚本，再执行以安装本机组件：
cat work/native-tools/node_modules/opencode-ai/postinstall.mjs
(cd work/native-tools/node_modules/opencode-ai && node postinstall.mjs)
```

安装 Codex 的脚本校验 npm 包完整性。测试本机网络回环与沙箱的检查需要允许运行，不是模型请求。首次联网下载依赖与 tokenizer 缓存不属于离线推理调用。

## 任务准备与检查

完整 300 项检查还依赖本机保留的旧 C1 历史题包，以及未公开的外部十题材料；这些夹具不随仓库发布。下面的生成命令准备自编的当前题目，不能单独替代全部历史夹具。公开 checkout 可直接运行 README 中的核心循环测试。`check_offline.py` 的完整评测许可适用于具备全部夹具和固定依赖的评测环境。

自编业务题生成器含参考实现和固定评分代码，它们用于开发与复现，不能进入执行 Agent 的可见工作区。未随仓库发布外部附件、公司 Skill、简历、账号或历史答案。

```bash
.venv/bin/python scripts/prepare_connect.py
.venv/bin/python scripts/prepare_candidate.py
.venv/bin/python scripts/prepare_longchain.py
.venv/bin/python scripts/prepare_highchain.py
.venv/bin/python scripts/prepare_business.py
.venv/bin/python scripts/check_offline.py
```

生成文件位于被忽略的 evaluation/private/；公开参考解使这些题更适合开发复现，而非对可能读过仓库的模型进行盲测。外部十题不随仓库发布，须自行获得有权使用的原材料，并通过 GOALKEEPER_SHARE_BENCH_SOURCE 指定读取目录，生成后的实际任务仍在当前 checkout 内运行。

## 真实评测

必须先取得当前源码、测试、配置、锁文件与题包对应的离线证明，再授权自己的账户。CLI 不会因安装或离线检查自动启动模型。

```bash
.venv/bin/python scripts/run_real.py --mode connect --allow-real-model --unlimited-tokens --transient-retries 3
.venv/bin/python scripts/run_real.py --mode candidate --candidate L1-v1 --allow-real-model --unlimited-tokens --transient-retries 3
```

CONNECT 四组通过后才能运行候选题；同题四组并行，共享模型与推理强度，每组独立目录、会话和计量。允许临时请求恢复，不重置截止时间。质量失败保留产物和消耗，未知用量不能记零，比较效率时只用双方完整通过且用量完整的同题记录。

本次公开发布未发送真实模型请求、未重跑评测。桌面端不在当前发布范围，后续完成后补充下载和使用文档。
