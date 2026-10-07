"""Prepare a small local task without authentication or model inference."""
import json

from goalkeeper.models import Task
from goalkeeper.paths import PROJECT


def prepare():
    destination = PROJECT / "examples/generated"
    if destination.exists():
        raise ValueError("示例目录已存在；请使用已有任务，不覆盖它。")
    root = destination / "initial"
    root.mkdir(parents=True)
    (root / "summarize.py").write_text(
        "def summarize(rows):\n    raise NotImplementedError('Implement the task')\n", encoding="utf-8"
    )
    (root / "check.py").write_text(
        "from copy import deepcopy\n"
        "from summarize import summarize\n"
        "rows = [{'category': ' books ', 'amount': 20}, {'category': 'books', 'amount': 5}, "
        "{'category': 'food', 'amount': 9}]\n"
        "before = deepcopy(rows)\n"
        "assert summarize(rows) == {'books': 25, 'food': 9}\n"
        "assert rows == before\n"
        "assert summarize([]) == {}\n"
        "for invalid in [[{'category': '', 'amount': 1}], [{'category': 'x', 'amount': True}], "
        "[{'category': 'x', 'amount': '1'}]]:\n"
        "    try:\n        summarize(invalid)\n"
        "    except ValueError:\n        pass\n"
        "    else:\n        raise AssertionError('invalid input must be rejected')\n"
        "print('DEMO_CHECK_PASSED')\n", encoding="utf-8"
    )
    task = Task(
        id="category-amount-demo",
        objective="实现 summarize(rows)：处理分类首尾空格，同分类整数金额相加，返回按分类排序的字典。",
        constraints=["只使用 Python 标准库；不修改 check.py；不调用网络。"],
        acceptance=[
            "不修改输入；空数组返回空字典；空分类、非整数金额和 bool 金额抛 ValueError。",
            "实现函数，添加自己的测试和 README，并通过 check.py。",
        ],
        write_paths=["summarize.py", "tests/", "README.md"],
        protected_paths=["check.py"],
        checks=[{"id": "demo-check", "argv": [str(PROJECT / ".venv/bin/python"), "-B", "check.py"],
                 "timeout": 10}],
    )
    file = destination / "task.json"
    file.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"task": str(file), "source": str(root), "real_model_calls": 0}, ensure_ascii=False))
    return file, root


if __name__ == "__main__":
    prepare()
