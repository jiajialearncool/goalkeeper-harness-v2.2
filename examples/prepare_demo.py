"""Create a tiny natural-language task; no account, network, or model is used."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = '读取 sales.csv，生成 totals.csv，按品类汇总销售金额，并说明结果。'


def main():
    output = ROOT / 'examples/generated'
    source = output / 'initial'
    source.mkdir(parents=True, exist_ok=True)
    (source / 'sales.csv').write_text('category,amount\n图书,120\n文具,30\n图书,80\n文具,20\n', encoding='utf-8')
    task = {'id': 'sales-demo', 'objective': MESSAGE, 'constraints': ['仅使用给定模拟数据。'],
            'write_paths': ['./'], 'delivery_mode': 'interactive'}
    (output / 'task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('已准备自然语言任务：' + MESSAGE)
    print('任务文件：examples/generated/task.json；模拟数据：examples/generated/initial/sales.csv')
    print('delivery_mode=interactive；没有预设 acceptance/checks。尚未调用模型。')


if __name__ == '__main__':
    main()
