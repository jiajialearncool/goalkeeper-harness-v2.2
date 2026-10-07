"""Validate disclosed graders on private synthetic references, never ship answers to agents."""

import asyncio
import json
import shutil

from goalkeeper.evaluation import score
from goalkeeper.paths import PROJECT
from goalkeeper.storage import save_report


async def main():
    destination = PROJECT / "outputs/share-bench-preparation/grader-validation"
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for index in range(1, 11):
        package = PROJECT / "evaluation/private" / f"SB10-{index:02}"
        root = destination / f"reference-{index:02}" / "workspace"
        if root.exists():
            raise ValueError("reference validation already exists")
        shutil.copytree(package / "initial", root)
        if index in {1, 4}:
            path = root / "xiaoman/web/api.py"
            text = path.read_text().replace('"begin": r["start_utc"],', '"start": r["start_utc"],')
            if index == 1:
                text = text.replace(
                    '"start": r["start_utc"],\n            "end"',
                    '"start": r["start_utc"],\n            "week_index": index + 1,\n            "end"',
                )
                text = text.replace(
                    "for r in queries.staff_week_shifts(conn, staff_id, monday)",
                    "for index, r in enumerate(queries.staff_week_shifts(conn, staff_id, monday))",
                )
            path.write_text(text)
        if index <= 3:
            path = root / "xiaoman/templates/print.html"
            path.write_text(path.read_text().replace("$footer", "$footer 仅供门店内部张贴"))
        if index == 2:
            path = root / "xiaoman/cli.py"
            text = path.read_text().replace(
                "    print(tomorrow_digest(conn, lang=args.lang))",
                '    print("[QH] remind hours=" + str(args.hours), file=sys.stderr)\n    if args.hours is None:\n        print(tomorrow_digest(conn, lang=args.lang))\n    else:\n        from datetime import timedelta\n        now = now_local()\n        for r in queries.shifts_starting_between(conn, now, now + timedelta(hours=args.hours)):\n            print(r["staff_name"], fmt_hm(parse_utc(r["start_utc"])))',
            )
            text = text.replace(
                "    p.set_defaults(func=cmd_remind)",
                '    p.add_argument("--hours", type=float)\n    p.set_defaults(func=cmd_remind)',
            )
            path.write_text(text)
        if index == 3:
            path = root / "xiaoman/exports/csv_export.py"
            text = path.read_text()
            start = text.index("HEADER = ")
            end = text.index("\n", start)
            text = (
                text[:start]
                + "HEADER = ['Store','Date','Weekday','Name','Shift','Role','Start','End','Note','StaffId']"
                + text[end:]
            )
            text = text.replace(
                '                r["note"],', '                r["note"],\n                r["staff_id"],'
            )
            path.write_text(text)
        if index == 4:
            path = root / "xiaoman/web/app.py"
            path.write_text(
                path.read_text()
                + """\noriginal_route = route

def route(conn, method, path, query, environ):
    if method == 'POST' and path == '/leave':
        form=_form(environ)
        conn.execute("INSERT INTO leave_requests(staff_id,date,reason,status,created_at) VALUES(?,?,?,'pending','2026-10-07T00:00:00Z')",(int(form['staff_id']),form['date'],form['reason']))
        return _redirect('/staff/'+form['staff_id'])
    if method == 'POST' and path == '/settings':
        form=_form(environ)
        if form.get('action')=='approve_leave':
            conn.execute("UPDATE leave_requests SET status='approved',decided_at='2026-10-07T01:00:00Z' WHERE id=?",(form['leave_id'],))
            return _redirect('/settings')
    status,headers,body=original_route(conn,method,path,query,environ)
    if method=='GET' and path=='/staff/2':
        body += b'<form method="post" action="/leave"><input name="staff_id" value="2"><input name="date" type="date"><textarea name="reason"></textarea><button>Submit</button></form>'
    if method=='GET' and path=='/settings':
        for row in conn.execute("SELECT id FROM leave_requests WHERE status='pending'"):
            body += ('<form method="post" action="/settings"><input name="leave_id" value="'+str(row['id'])+'"><button name="action" value="approve_leave">Approve</button></form>').encode()
    approved=[dict(r) for r in conn.execute("SELECT lr.*,st.name FROM leave_requests lr JOIN staff st ON st.id=lr.staff_id WHERE lr.status='approved'")]
    if method=='GET' and path=='/schedule':
        text=body.decode()
        for row in approved:
            marker='<section class="day" data-date="'+row['date']+'">'
            text=text.replace(marker,marker+row['name']+'请假')
        body=text.encode()
    if method=='GET' and path.startswith('/api/'):
        data=json.loads(body)
        if isinstance(data,list):
            for item in data:
                if isinstance(item,dict) and ('start' in item):
                    item['leave_requests']=[r for r in approved if r['staff_id']==item.get('staff_id',2)]
            body=json.dumps(data,ensure_ascii=False).encode()
    return status,headers,body
"""
            )
        if index == 5:
            check_text = (PROJECT / "scripts/share_bench_checks.py").read_text()
            start = check_text.index("def jsonl_expected(")
            end = check_text.index("\ndef run_jsonl(", start)
            reference = (
                "import json\nimport datetime as dt\nfrom pathlib import Path\n" + check_text[start:end]
            )
            reference += "\nrows=jsonl_expected(Path('data/raw.jsonl').read_bytes())\nPath('out').mkdir(exist_ok=True)\nPath('out/events.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\\n' for r in rows),encoding='utf-8')\n"
            (root / "process.py").write_text(reference)
        if index in {6, 7}:
            (root / "reply.md").write_text(
                "Your basic economy reservation cannot be changed or cancelled under the policy. A refund, gift card or travel voucher is not permitted. Your reservation remains unchanged."
            )
        if index == 8:
            (root / "remove_occ.py").write_text(
                "def remove_occ(string,char,all=False):\n    if not isinstance(char,str) or len(char)!=1: raise ValueError('Second parameter must be a single character.')\n    if all: return string.replace(char,'')\n    positions=[i for i,c in enumerate(string) if c==char]\n    removed=set(positions[:1]+positions[-1:])\n    return ''.join(c for i,c in enumerate(string) if i not in removed)\n"
            )
        if index == 9:
            (root / "scheduler.py").write_text(
                "class TaskScheduler:\n    def add_task(self): pass\n    def remove_task(self): pass\n    def execute_tasks(self): pass\n"
            )
        if index == 10:
            (root / "reply.md").write_text(
                "黄鹤楼是武汉旅游的重要景点。颐和园在北京，超出了武汉旅游范围，无法在此介绍。附近美食可以考虑热干面；早餐可以体验武汉的过早文化。本回复不涉及酒店或票务预订。"
            )
        verdict = await score(root, package / "grade.py", destination / f"score-{index:02}")
        results.append(dict(task=package.name, verdict=verdict))
        print(package.name, verdict["passed"], flush=True)
        if not verdict["passed"]:
            print(json.dumps(verdict, ensure_ascii=False), flush=True)
    save_report(destination / "results.json", dict(model_calls=0, results=results))
    if not all(row["verdict"]["passed"] for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
