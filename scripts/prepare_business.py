"""Author four resume-inspired simulations, with frozen contracts and fresh answers."""

import importlib.util
import json
from pathlib import Path

from goalkeeper.models import Task
from goalkeeper.paths import PROJECT, inside_project


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GENERAL = """
这是已有本地Python工具的连续改造，使用标准库，不联网、不调用题内模型。util.py已有JSON读取、原子保存和legacy_summary；旧CLI summary及tests/test_legacy.py必须保持。允许修改业务包、增加tests、README.md、analysis.json和REPORT.md；公开检查、样例数据及旧测试受保护。
所有函数不修改输入，返回嵌套对象深复制。所有ID为非空且无首尾空格字符串；计数、金额、时间、版本必须int而非bool且非负；时间单位秒。文档明确的非法参数、冲突、未知枚举、未知业务ID抛ValueError。字段必须恰为所列required加optional，没有隐含参数；可选字段说明未给出时不能自行推断。
CLI固定为 python -m {package}.cli COMMAND INPUT_JSON_PATH [--save OUTPUT_JSON_PATH]。INPUT始终是UTF-8 JSON文件路径，不接收内联JSON。无--save时输入与文件系统不变；有--save时先完整计算，成功后用util.save同目录临时文件flush/fsync/os.replace原子保存。错误保留原输出并清理临时文件。成功exit0、stderr空、stdout恰一行JSON；业务错误/JSON错误/文件错误exit2、stdout空、stderr非空。COMMAND参数始终字符串，不存在数字位置参数。
legacy_summary(rows)保留旧返回{count,total}，rows是[{id,value}]且value为非负整数。CLI summary直接接受该数组。新模块core.py的公开函数签名见下文，可自由拆分模块，但签名与返回结构保持。
补充至少一个业务unittest文件（旧test_legacy.py不算新增），运行 python -m unittest discover -s tests -v。公开检查可通过 .venv解释器 public_initial.py、public_change.py 执行；--probe仅用于统一追加需求触发，不计作完整验收。
analysis.json为固定data/sample.json经其指定command运行的完整JSON结果；REPORT.md为至少120个字符的业务说明，包含样例证据标识、原因/结果、边界与复现命令。analysis.json不要求包含后续追加演示，但追加功能必须有测试。README保留完整接口、数据约束及CLI示例。私有评分使用不同数据检查已公开规则，不要求猜测隐藏业务条件。
"""

SPECS = {
    "B1-v1": dict(
        package="badcase",
        reference="BADCASE",
        title="查清点击不感兴趣后同类商品仍出现的原因，并完善诊断工具",
        functions={
            "normalize": "raw",
            "diagnose": "raw,query",
            "batch": "raw,queries",
            "compare": "before,after,queries",
            "dispatch": "command,p",
        },
        body="""
## 推荐排查数据及归因
normalize(raw)输入恰含schema/requests/feedback/products，后三者数组，schema整数1或2。products行恰含sku/category/spu，sku唯一；category/spu规范ID或null。feedback行恰含id/user/sku/at/kind，id唯一、sku存在；kind初始支持dislike/bought/privacy，按category匹配，有效期分别180/30/365天。at是反馈发生时间。
schema1请求行恰含id/user/at/placement/items/profile_ok/joined/trace。schema2行恰含request_id/user_id/timestamp/slot/results/signals，signals恰含profile_ok/joined/trace；对应schema1字段逐项转换。profile_ok为bool或null（未知），joined/trace严格bool；items/results是[{sku,source,hit}]，同请求sku唯一且存在于products，source=organic或ad，hit=bool或null。请求ID唯一。
normalize返回{schema:2,requests:规范schema1字段数组,feedback:数组,products:以sku为键的产品字典}；请求和反馈分别按(at,id)排序。这是内部格式，normalize只接收原始输入，不要求再次接收自己的输出。
diagnose(raw,query)中query恰含user/sku/at/placement，sku必须存在。精确匹配user/at/placement，多个请求匹配抛ValueError；无请求返回{reason:'no_request',request_id:null,feedback_ids:[],evidence:[],confirmed:false}。
有效反馈须同user、feedback.at<=request.at且差值<有效天数*86400，边界到期即失效；问题商品和反馈商品category相等才匹配。有效期内相关产品category缺失记missing，不把null等同相等。feedback_ids按反馈(at,id)排序；evidence恰为[{source:'requests',id:请求ID},...]，随后加入所有匹配反馈的{source:'feedback',id:反馈ID}。
每个诊断恰含reason/request_id/feedback_ids/evidence/confirmed。按以下优先级选择reason：商品不在结果→not_returned/true；广告→ad_route/true；trace=false→insufficient_trace/false；商品hit=true但在返回结果→inconsistent_trace/false；没有匹配有效反馈→missing_product_relation/false（若存在missing）否则no_active_feedback/true；profile_ok=null→unknown_profile/false；profile_ok=false→profile_unavailable/true；joined=false→route_not_integrated/true；其他→rule_violation/true。hit=null本身不影响上述已明确的分支。不得把“无详细日志”“未知画像”说成确定的过滤错误。
batch(raw,queries)按输入query顺序诊断，queries必须数组；compare(before,after,queries)返回[{index,before:reason,after:reason,changed:两个完整诊断对象不相等}]。
dispatch支持summary/normalize/diagnose/batch/compare；normalize载荷为raw，diagnose为{data,query}，batch为{data,queries}，compare为{before,after,queries}。
REPORT.md至少引用R和F，并说明实际样例证据与结论，区分诊断工具解析修复和线上推荐策略修复。样例属于模拟，不引用公司接口或真实用户。
""",
        change="""新增反馈kind=image，按商品spu（同款标识）匹配，有效期365天；不能按category代替。产品spu缺失时遵循missing_product_relation规则。原dislike/bought/privacy类目匹配和有效期、新旧日志、广告路由、证据不足判断全部保留。补充同类不同款、不同类同款、有效期边界与旧规则回归测试。""",
    ),
    "B3-v1": dict(
        package="experiments",
        reference="EXPERIMENT",
        title="解释实验整体指标改善但部分用户体验下降，并升级分析口径",
        functions={
            "normalize": "raw",
            "analyze": "raw,query",
            "compare": "raw,before,after",
            "dispatch": "command,p",
        },
        body="""
## 实验关联与统计
normalize(raw)恰含schema/assignments/events，schema整数1或2，其余数组。assignments行恰含user/group/cohort/version/start/end；group只能control/treatment，end>start；同user分流窗口不得重叠，允许相接，窗口[start,end)。版本与人群都是规范ID。
schema1事件恰含id/user/at/version/returned/clicked/converted，后三者bool，converted要求clicked=true，clicked要求returned=true。schema2事件恰含event_id/user_id/timestamp/strategy/stages，stages恰含returned/clicked/converted，逐项转为schema1字段。相同事件ID且内容完全一致去重，不一致拒绝。normalize返回schema2标记但事件使用规范schema1字段，assignments按(user,start)排序，events按(at,id)排序；不要求normalize再次接收内部输出。
analyze(raw,query)恰有start/end/version，可选weights；初始统计unit为request。窗口[start,end)，end>start。先按事件时间关联所属分流记录；无记录计excluded.unassigned，有记录但assignment.version!=query.version或event.version!=assignment.version计excluded.version；窗口外不计。只有关联成功才进入统计。
每个metric恰含requests/returned/clicked/converted/return_rate/ctr/cvr/conversion_rate，前三种阶段计数bool真值，requests为事件数；return_rate=returned/requests，ctr=clicked/returned，cvr=converted/clicked，conversion_rate=converted/requests。比例一律用最简整数数组[分子,分母]，0/n=[0,1]，分母0返回null，禁止浮点近似。
返回恰含unit:'request',groups,cohorts,harmed_cohorts,excluded,evidence_ids,standardized。groups恰有control/treatment，各为metric；cohorts恰有这两组，再以全部匹配事件中出现的人群名为键（排序），缺少该组事件的人群仍给空metric。harmed_cohorts是两组都有非null conversion_rate且treatment严格低于control的人群名升序；evidence_ids为计入事件ID升序。
weights缺省standardized=null；给定时为非空人群ID→正整数权重字典（非bool），要求每个给定人群在两组都有非null conversion_rate；standardized={control:[最简分子,分母],treatment:[...]}，各组按这些固定权重加权conversion_rate并除以权重合计。无需所有出现人群都列入weights，未列入人群不参与加权。
compare(raw,before,after)返回{before:analyze结果,after:analyze结果}。dispatch summary/normalize/analyze/compare，normalize载荷raw，analyze为{data,query}，compare为{data,before,after}。
REPORT.md至少解释easy/hard两个人群，引用样例总体和统一权重后结果，不能仅据总体改善判断所有人群受益。
""",
        change="""query新增可选unit='request'或'user'，缺省request，其他值拒绝。user口径必须先完成窗口、分流和版本过滤，再对每个user选择最后一条有效事件（按(at,id)最大；同时间ID字典序较大者）；随后按该条事件的group/cohort计算同样metric、weights、harmed_cohorts与evidence_ids。requests字段此时表示选中的用户事件数。excluded仍统计去重前规范事件的排除数。compare应支持请求口径与用户口径并排对比。保留默认request结果，补充重复访问、同时间决胜和用户迁移分组的测试。""",
    ),
    "B6-v1": dict(
        package="retirement",
        reference="RETIREMENT",
        title="安全下线特征并在发现遗漏引用后恢复业务",
        functions={
            "validate": "raw",
            "plan": "state,targets",
            "apply_batch": "state,envelope",
            "preview": "state,envelope",
            "reconcile": "state",
            "dispatch": "command,p",
        },
        body="""
## 特征状态与下线流程
state恰含revision/features/consumers/data/backup/batches/events；初始revision0，日志与batches空数组，backup空字典。features按特征ID为键，行恰含depends/status/verified；depends是唯一已存在ID数组，不能自引用或形成环；status=active/retiring/retired，verified是bool。consumers为业务ID→唯一特征ID数组，data为每个特征ID→记录数组（记录为任意JSON），backup为清理前记录备份字典。features的每个ID必须有data行。validate校验上述结构并返回深复制；任意status与未知依赖非法，不能自动改正。
batches唯一规范request_id数组；长度与events长度等于revision，event恰含revision/request_id/actions，revision从1连续，request_id与batches相应项一致。
plan(state,targets)要求非空、唯一已存在特征ID数组。递归纳入所有未retired且依赖所选特征的特征；返回{order,blockers,safe}。order必须先依赖者后被依赖者：每轮挑出剩余集合内没有其他特征依赖它的全部ID，升序加入，再进入下一轮；blockers是所有引用集合内特征的业务ID升序，safe=blockers为空。
apply_batch(state,envelope)先validate；envelope含规范request_id。已提交的ID直接返回当前深复制，忽略其余坏字段，不加日志。新ID要求恰含request_id/expected_revision/actions，版本精确相等，actions数组可为空。顺序执行在临时对象，任一步失败整批无副作用；成功revision+1，追加batch与event（actions深复制）。
动作字段严格如下：detach {kind,consumer}删除已存在业务；attach {kind,consumer,features}新建/替换该业务引用，所有引用必须active且唯一（可空）。其他动作恰含kind/feature，feature存在：mark要求active，变retiring并verified=false；clean要求retiring且没有该特征backup，备份data再置data=[]；verify要求retiring、已有backup且data=[]，置verified=true；finish要求retiring、verified=true且data=[]，置retired，保留backup。mark/clean/verify/finish任一执行前都拒绝仍有consumer直接引用、或存在未retired的特征依赖它。禁止跳过这些状态条件。
preview(state,envelope)返回{base_revision,next_revision,changed,replayed}，changed为最终feature对象或data变化的ID升序；replayed表示revision未增加。reconcile(state)返回{active,retiring,retired,live_rows,backup_rows,revision}，状态数组升序，记录数是各data/backup数组长度之和。
dispatch summary/validate/plan/apply/preview/reconcile；validate/reconcile载荷直接state，plan载荷{state,targets}，apply/preview载荷{state,envelope}。CLI演示：python -m retirement.cli plan data/sample.json --save analysis.json。REPORT.md解释A/B/C依赖、biz阻止原因、安全顺序、恢复边界。
""",
        change="""新增rollback动作，恰含kind:'rollback'/feature；仅允许retiring特征（active或retired拒绝）。若有backup则恢复完整data并删除该backup，无backup保留原data；status置active、verified置false。rollback用于发现遗漏引用后撤回尚未完成的下线，不适用mark/clean等的引用检查。可以同批rollback后attach回业务，之后plan必须再次报告该业务阻止下线；任一步失败整批无副作用。回放不重复恢复或改日志，补充清理后恢复、核对后恢复、非法再接入失败的测试与CLI演示。""",
    ),
    "B11-v1": dict(
        package="receipts",
        reference="RECEIPTS",
        title="完善小票审核与奖励流程并处理复审改判",
        functions={
            "document": "raw",
            "fingerprint": "raw",
            "validate": "raw",
            "apply_batch": "state,envelope",
            "preview": "state,envelope",
            "reconcile": "state",
            "dispatch": "command,p",
        },
        body="""
## 小票、审核和奖励
document(raw)恰含merchant/date/number/items。前三者非空字符串、去首尾空格并casefold；date仅作非空字符串，不要求日历合法性。items非空数组，行恰含name/quantity/unit_price；name规范ID、quantity正整数、unit_price非负整数分。返回规范文档深复制。fingerprint(raw)为规范merchant/date/number这三个字段用json.dumps(ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')取SHA256十六进制，不包含items。相同票号商家日期视为同一小票，OCR金额纠错不改变身份。
state恰含revision/receipts/wallets/ledger/batches/events。receipts以ID为键，行恰含id/user/at/document/status/version/reward，id等于键，user规范ID，at提交秒数，version/reward非负整数，status=pending/approved/rejected。wallets为用户ID→非负奖励积分。初始revision0、receipts/wallets空字典，ledger/batches/events空数组。validate校验结构及document，返回深复制；不要求validate校验奖励投影（由reconcile负责）。batches唯一规范request_id，events与batches长度均等于revision；event恰含revision/request_id/actions，revision连续，ID与batches对应。
apply_batch先validate，request_id已提交则返回当前深复制并忽略其他坏字段；新envelope恰含request_id/expected_revision/actions，版本必须相等，actions数组可空。整批顺序执行，任一步失败无副作用。
submit动作恰含kind:'submit'/id/user/at/document，新ID唯一，创建pending/version0/reward0；允许同指纹重复上传。correct恰含kind:'correct'/id/expected_version/document，仅pending且票版本相等时纠正OCR文档并version+1。review恰含kind:'review'/id/expected_version/status，仅pending且版本相等，status只能approved/rejected，更新status并version+1；已审核不能再次review。
完成所有动作后，统一重算奖励：所有approved票按(at,id)升序，相同fingerprint仅最早approved票获奖，跨用户也去重；奖励积分=sum(quantity*unit_price)//100，不用浮点，允许0分；其余票0。按receipt ID升序，对新奖励与批前reward之差产生非零ledger行，恰含receipt_id/user/delta/revision/request_id，delta可负，revision为本批新版本。wallets保留所有已上传用户（含0分），按该用户当前reward求和。成功revision+1并追加batch/event。
preview返回{base_revision,next_revision,wallet_delta,replayed}；wallet_delta含批前批后用户并集全部用户（含0差值）按ID排序。reconcile重新计算奖励，检查每张reward、wallets和ledger按用户sum(delta)一致；任何不一致抛ValueError，返回{revision,receipts:总数,approved:审核通过数,rewarded:正奖励票数,total_reward,wallets:按user排序}。ledger行恰为前述字段，delta严格int，未知用户不能有账目；其余字段使用规范ID/非负整数。
dispatch summary/document/fingerprint/validate/apply/preview/reconcile；document/fingerprint载荷raw，validate/reconcile载荷state，apply/preview为{state,envelope}。REPORT.md引用小票A和用户U，说明身份判重、审核、整数奖励及复审造成的账目变化。奖励为模拟积分，不是真实支付。
""",
        change="""新增revise动作，恰含kind:'revise'/id/expected_version/status；仅已审核（approved或rejected）且版本相等的票允许改判，status只能approved/rejected，每次version+1，即使同status也允许。批末按原规则重算所有同指纹票奖励：最早票改为rejected时撤回它的奖励并补给下一个approved票；重新approved时恢复最早票归属，撤回后来票奖励。账目必须记录整数正负差额且钱包不负、不重复发奖。回放保持全部对象不变，补充跨用户转移、连续改判、旧版本冲突、核对及CLI测试。""",
    ),
}

SPECS = {
    key.replace("-v1", "-v2"): {
        **value,
        "body": value["body"]
        .replace("analysis.json", "artifacts/analysis.json")
        .replace("REPORT.md", "artifacts/REPORT.md"),
    }
    for key, value in SPECS.items()
}
GENERAL = GENERAL.replace("analysis.json", "artifacts/analysis.json").replace(
    "REPORT.md", "artifacts/REPORT.md"
)

CLI = r"""import argparse,json,sys
from . import core,util
def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('command');parser.add_argument('input');parser.add_argument('--save');a=parser.parse_args(argv)
    try:
        result=core.dispatch(a.command,util.read(a.input))
        if a.save:util.save(result,a.save)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ValueError,OSError,KeyError,TypeError) as e:print(str(e),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def prepare(task_id, package=None):
    spec = SPECS[task_id]
    source_id = task_id.replace("-v2", "-v1")
    package = inside_project(package or PROJECT / "evaluation/private" / task_id)
    if package.exists():
        raise ValueError("题包已存在，不覆盖历史版本")
    ref = sibling("business_reference")
    checks = sibling("business_checks")
    name, common, cases, initial, change, probe = checks.CONFIG[source_id]
    description = (
        "# " + task_id + "：" + spec["title"] + "\n" + GENERAL.replace("{package}", name) + spec["body"]
    )
    # Baseline command and tests are working. New interfaces are explicit extension points.
    stub = "from .util import legacy_summary\n\n" + "".join(
        "def " + fn + "(" + args + "):\n    raise NotImplementedError\n\n"
        for fn, args in spec["functions"].items()
        if fn != "dispatch"
    )
    stub += "def dispatch(command,p):\n    if command=='summary':return legacy_summary(p)\n    raise NotImplementedError\n"
    legacy_test = (
        "import unittest\nfrom "
        + name
        + ".util import legacy_summary\nclass LegacyTests(unittest.TestCase):\n    def test_summary(self):self.assertEqual(legacy_summary([{'id':'A','value':2}]),{'count':1,'total':2})\n    def test_bool(self):\n        with self.assertRaises(ValueError):legacy_summary([{'id':'A','value':True}])\n"
    )
    artifact_assert = {
        "B1-v1": "same(json.loads(Path('analysis.json').read_text()),{'reason':'route_not_integrated','request_id':'R','feedback_ids':['F'],'evidence':[{'source':'requests','id':'R'},{'source':'feedback','id':'F'}],'confirmed':True});tokens=['R','F']",
        "B3-v1": "a=json.loads(Path('analysis.json').read_text());same(a['harmed_cohorts'],['easy','hard']);same(a['standardized'],{'control':[3,4],'treatment':[7,16]});same(a['groups']['control']['requests'],10);tokens=['easy','hard']",
        "B6-v1": "same(json.loads(Path('analysis.json').read_text()),{'order':['B','C','A'],'blockers':['biz'],'safe':False});tokens=['A','B','C','biz']",
        "B11-v1": "a=json.loads(Path('analysis.json').read_text());same(a['wallets'],{'U':24});same(a['receipts']['A']['reward'],24);same(a['revision'],1);tokens=['A','U']",
    }[source_id]
    cases["artifacts"] = (
        "可复现分析结果与业务报告",
        artifact_assert
        + ";text=Path('REPORT.md').read_text();assert len(text)>=120 and all(t in text for t in tokens)",
    )
    for stage in ("initial", "reference"):
        base = package / stage
        write(base / "artifacts/.keep", "")
        write(base / name / "__init__.py", "")
        write(base / name / "util.py", ref.UTIL)
        write(base / name / "core.py", stub if stage == "initial" else getattr(ref, spec["reference"]))
        write(base / name / "cli.py", CLI)
        write(base / "tests/test_legacy.py", legacy_test)
        write(
            base / "README.md",
            description + ("\n## 追加要求\n" + spec["change"] if stage == "reference" else ""),
        )
    allkeys = [k for k in cases if k != "artifacts"]
    ref_tests = (
        checks.source(source_id, allkeys)
        + "\nclass BusinessTests(unittest.TestCase):\n"
        + "".join("    def test_" + k + "(self):check_" + k + "()\n" for k in allkeys)
    )
    write(package / "reference/tests/test_business.py", ref_tests)
    # These fixture-building functions create author-written data, never call a model.
    scope = {}
    exec("import copy\n" + common, scope)
    d = scope["data"]()
    if source_id == "B1-v1":
        sample = {"data": scope["v2"](d), "query": scope["query"]()}
        command = "diagnose"
    elif source_id == "B3-v1":
        sample = {"data": scope["v2"](d), "query": scope["query"](weights={"easy": 1, "hard": 1})}
        command = "analyze"
    elif source_id == "B6-v1":
        sample = {"state": d, "targets": ["A"]}
        command = "plan"
    else:
        sample = {"state": d, "envelope": scope["env"](d, [scope["submit"](), scope["review"]()])}
        command = "apply"
    for stage in ("initial", "reference"):
        write(package / stage / "data/sample.json", json.dumps(sample, ensure_ascii=False, indent=2))
    description += (
        "\n固定样例运行命令：python -m "
        + name
        + ".cli "
        + command
        + " data/sample.json --save artifacts/analysis.json\n"
    )
    for stage in ("initial", "reference"):
        old = (package / stage / "README.md").read_text()
        write(
            package / stage / "README.md",
            old
            + "\n固定样例运行命令：python -m "
            + name
            + ".cli "
            + command
            + " data/sample.json --save artifacts/analysis.json\n",
        )
    # Reference imports are isolated by stage name; no reference files enter initial workspaces.
    modspec = importlib.util.spec_from_file_location(
        "authored_" + task_id.replace("-", "_"),
        package / "reference" / name / "__init__.py",
        submodule_search_locations=[str(package / "reference" / name)],
    )
    mod = importlib.util.module_from_spec(modspec)
    import sys

    sys.modules[modspec.name] = mod
    modspec.loader.exec_module(mod)
    from importlib import import_module

    core = import_module(modspec.name + ".core")
    write(
        package / "reference/artifacts/analysis.json",
        json.dumps(core.dispatch(command, sample), ensure_ascii=False, indent=2),
    )
    report = spec["title"] + "\n这是独立编写的模拟案例。证据来自固定样例data/sample.json，复现命令见README。"
    report += {
        "B1-v1": "请求R和反馈F显示同类有效反馈存在、画像可用但召回链路未接入，因此不能把此例说成画像缺失。广告与缺少详细日志须单独处理。",
        "B3-v1": "easy与hard两个人群都下降，但人群构成变化使总体改善。统一权重后control为3/4、treatment为7/16，不能只看总体。",
        "B6-v1": "A由B与C依赖，先下线B和C再处理A。biz仍引用B，当前计划不安全。必须解除引用，清理并核对后完成；未完成时可恢复。",
        "B11-v1": "A属于用户U，审核通过后金额2468分对应24积分。身份指纹忽略OCR明细，重复票不重复发奖，复审改判须转移奖励并保持账目一致。",
    }[source_id]
    report += "\n结论只适用于给定模拟材料，缺失信息须保留未知；工具兼容、版本冲突和输入隔离均由独立测试验证。"
    write(package / "reference/artifacts/REPORT.md", report)
    protected = [
        "public_initial.py",
        "public_change.py",
        "public_legacy.py",
        "data/sample.json",
        "tests/test_legacy.py",
        "artifacts/.keep",
    ]
    write(package / "initial/public_initial.py", checks.public(source_id, initial, probe))
    write(package / "initial/public_change.py", checks.public(source_id, change))
    write(package / "initial/public_legacy.py", checks.public(source_id, ["legacy"]))
    write(
        package / "grade.py",
        checks.grade(source_id)
        .replace("Path('analysis.json')", "Path('artifacts/analysis.json')")
        .replace("Path('REPORT.md')", "Path('artifacts/REPORT.md')"),
    )
    python = str(PROJECT / ".venv/bin/python")
    task = Task(
        id=task_id,
        objective=spec["title"]
        + "。先阅读README.md完整契约，连续改造已有工具，交付业务包、项目测试、artifacts/analysis.json和artifacts/REPORT.md。",
        constraints=[
            "只用标准库和本地模拟数据，保留旧接口/CLI/测试，不修改受保护文件。",
            "每个函数、返回字段、边界、CLI文件参数及保存规则按README执行。",
        ],
        acceptance=[
            "全部公开检查及独立业务验收通过，固定样例结果可复现，报告引用具体证据；追加需求后原行为保持。"
        ],
        write_paths=[name + "/", "tests/", "README.md", "artifacts/"],
        protected_paths=protected,
        checks=[
            dict(id="legacy", argv=[python, "public_legacy.py"], timeout=30),
            dict(id="initial", argv=[python, "public_initial.py"], timeout=45),
        ],
        feedback=dict(
            probe=dict(id="business_probe", argv=[python, "public_initial.py", "--probe"], timeout=30),
            text=spec["change"],
            acceptance=["追加规则通过检查且旧功能与输出保留。"],
            checks=[dict(id="change", argv=[python, "public_change.py"], timeout=45)],
        ),
    )
    write(package / "task.json", task.model_dump_json(indent=2))
    write(package / "description.md", description + "\n## 中途追加要求\n" + spec["change"])
    write(
        package / "contract.json",
        json.dumps(
            dict(
                version=task_id,
                original_list_number=int(task_id[1:].split("-")[0]),
                origin="fresh authored local simulation; no company systems/data/helpers or historical answers",
                classification="high linked business integration candidate",
                human_duration_calibrated=False,
                time_seconds_per_group=1800,
                cumulative_token_limit=None,
                quality_dimensions={k: v[0] for k, v in cases.items()},
            ),
            ensure_ascii=False,
            indent=2,
        ),
    )
    return package


if __name__ == "__main__":
    for task_id in SPECS:
        print(prepare(task_id))
