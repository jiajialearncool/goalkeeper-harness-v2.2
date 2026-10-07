"""Run all local regressions and seal their source identity. Never logs in or calls a model."""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from uuid import uuid4

from goalkeeper.evaluation import execution_identity
from goalkeeper.paths import PROJECT
from goalkeeper.readiness import REQUIRED_TESTS, test_identity, package_identity
from goalkeeper.storage import save_report, fingerprint


def main():
    destination = PROJECT/'outputs/full-code-audit'/('verification-'+uuid4().hex)
    destination.mkdir(parents=True)
    before = [execution_identity(i) for i in range(4)]
    tests_before = test_identity()
    packages_before = package_identity()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
               TIKTOKEN_CACHE_DIR=str(PROJECT/'work/tokenizer-cache'))
    checks = [
        ([str(PROJECT/'.venv/bin/ruff'),'check','src','scripts','tests'], destination/'ruff.txt'),
        ([sys.executable,'-m','pytest','-q','tests','--basetemp='+str(destination/'temp'),
          '--junitxml='+str(destination/'tests.xml')], destination/'pytest.txt'),
    ]
    for command, output in checks:
        completed = subprocess.run(command,cwd=PROJECT,env=env,stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,text=True)
        output.write_text(completed.stdout)
        print(completed.stdout,flush=True)
        if completed.returncode:
            raise SystemExit(completed.returncode)
    cases = list(ET.parse(destination/'tests.xml').getroot().iter('testcase'))
    if any(case.find('skipped') is not None for case in cases):
        raise SystemExit('离线检查存在跳过项，未签发真实运行许可。')
    verified = {case.attrib['name'] for case in cases if not any(
        case.find(tag) is not None for tag in ('failure','error','skipped'))}
    if not REQUIRED_TESTS.issubset(verified):
        raise SystemExit('关键原生离线检查被跳过或缺失，未签发真实运行许可。')
    if (before != [execution_identity(i) for i in range(4)] or tests_before != test_identity()
            or packages_before != package_identity()):
        raise SystemExit('验收期间源码或测试发生变化，未签发真实运行许可。')
    evidence = [destination/'tests.xml',destination/'ruff.txt']
    certificate = PROJECT/'outputs/full-code-audit/offline-readiness.json'
    save_report(certificate,{'kind':'offline_readiness','passed':True,
        'execution_identities':before,'tests_hash':tests_before,
        'packages':packages_before,
        'verified_cases':sorted(verified),'model_calls':0,
        'evidence':[{'path':str(p),'hash':fingerprint(p.read_text())} for p in evidence]})
    print(str(certificate))


if __name__ == '__main__':
    main()
