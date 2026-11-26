"""Common conservative task selection, independent of the calling interface."""
import re


def task_profile(text):
    # A bounded source-based report is distinct from code execution and deep research.
    if (len(text) <= 180 and re.search(r'调研|研究|比较|对比', text)
            and re.search(r'报告|汇报|html', text, re.I)
            and not re.search(r'修改|修复|部署|深入|全面|医疗|投资|合同|附件|代码', text)):
        return 'research_report'
    # A short imperative is still an execution task. Unclear requests keep full depth.
    if len(text) > 180 or re.search(
        r'修改|修复|实现|搭建|部署|安装|删除|写入|生成|制作|创建|排查|诊断|审查|迁移|优化|'
        r'运行|执行|开发|附件|工作区|文件引用|报告|全面|详细|多来源|深入|医疗|投资|合同|'
        r'\.(?:pdf|docx|xlsx|py|js|ts)\b|\b(?:build|fix|write|run|install|deploy)\b', text, re.I
    ):
        return 'general'
    if re.search(r'[?？]|什么|怎么|区别|定位|天气|介绍|解释|比较|对比|\b(?:what|why|how|compare)\b', text, re.I):
        return 'short_qa'
    return 'general'


def reasoning_effort(text, profile):
    if profile == 'research_report':
        return 'medium'
    if profile != 'short_qa':
        return 'xhigh'
    return 'medium' if re.search(r'区别|比较|对比|定位|原因|为什么|\b(?:compare|why)\b', text, re.I) else 'low'


def resolve_policy(state):
    config = state.config
    if config.task_profile == 'auto':
        config.task_profile = task_profile(state.task.objective)
    if config.effort == 'auto':
        config.adaptive_effort = True
        config.effort = reasoning_effort(state.task.objective, config.task_profile)
    return config
