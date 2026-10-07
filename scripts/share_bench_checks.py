"""Local, disclosed checks for the supplied tasks; not their unavailable hidden graders."""

import ast
import csv
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urljoin
from html.parser import HTMLParser

ROOT = Path.cwd()
RESULTS = []
FROZEN_TESTS = {}
INITIAL_RESERVATIONS = None


def check(name, operation, dimension="requirements"):
    try:
        value = operation()
        if value is False:
            raise AssertionError("condition false")
        RESULTS.append(dict(id=name, dimension=dimension, passed=True))
    except Exception as error:
        RESULTS.append(
            dict(
                id=name,
                dimension=dimension,
                passed=False,
                detail=type(error).__name__ + ": " + str(error)[:800],
            )
        )


def load(path):
    spec = importlib.util.spec_from_file_location("subject_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(path, method="GET", form=None):
    from xiaoman.web.app import application

    path, _, query = path.partition("?")
    body = urlencode(form or {}).encode()
    env = dict(
        REQUEST_METHOD=method,
        PATH_INFO=path,
        QUERY_STRING=query,
        CONTENT_LENGTH=str(len(body)),
        CONTENT_TYPE="application/x-www-form-urlencoded",
        SERVER_NAME="testserver",
        SERVER_PORT="80",
        **{"wsgi.input": io.BytesIO(body), "wsgi.url_scheme": "http"},
    )
    output = {}

    def start(status, headers):
        output.update(status=status, headers=dict(headers))

    output["body"] = b"".join(application(env, start))
    assert output["status"].startswith(("200", "302", "303")), output["status"]
    return output


def api(path):
    return json.loads(request(path)["body"])


def staff():
    return api("/api/v1/staff/2/shifts?week=2026-10-12")


def all_shifts():
    return api("/api/v1/shifts?store=SH-JA&week=2026-10-12")


def footer():
    html = request("/print?store=SH-JA&week=2026-10-12")["body"].decode()
    assert re.search(
        r"<[^>]*class=[\"\'][^\"\']*print-footer[^\"\']*[\"\'][^>]*>.*?仅供门店内部张贴", html, re.S
    ), "footer phrase missing"


def alias_new():
    rows = staff()
    assert len(rows) == 3
    assert [r["week_index"] for r in rows] == [1, 2, 3]
    assert all("start" in r and "begin" not in r for r in rows)
    assert [r["start"] for r in rows] == sorted(r["start"] for r in rows)
    assert all(r["start"].endswith("Z") and r["end"].endswith("Z") for r in rows)
    assert len(api("/api/v1/staff/3/shifts?week=2026-10-12")) > 0
    assert api("/api/v1/staff/999/shifts?week=2026-10-12") == []


def legacy_api():
    rows = staff()
    assert len(rows) == 3 and all("begin" in r and r["begin"].endswith("Z") for r in rows), (
        "old v1 client requires begin"
    )


def jsonl_expected(raw):
    records = {}
    for index, line in enumerate(raw.removeprefix(b"\xef\xbb\xbf").splitlines()):
        try:
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                text = line.decode("gbk")
            obj = json.loads(text)
            if not isinstance(obj, dict) or not isinstance(obj.get("event_id"), (str, int, float)):
                continue
            stamp = obj["ts"]
            if isinstance(stamp, (int, float)):
                when = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
            else:
                when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    continue
                when = when.astimezone(dt.timezone.utc)
            payload = obj.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            nested = payload.get("user")
            nested = nested if isinstance(nested, dict) else {}
            if "id" in nested:
                uid = nested["id"]
            elif "uid" in payload:
                uid = payload["uid"]
            else:
                uid = obj["user_id"]
            action = payload["action"] if "action" in payload else obj["type"]
            eid = str(obj["event_id"])
            out = dict(
                event_id=eid,
                ts=when.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                user_id=uid,
                action=action,
            )
            if eid not in records or (when, index) > records[eid][:2]:
                records[eid] = (when, index, out)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            continue
    return [records[eid][2] for eid in sorted(records)]


def run_jsonl(raw):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shutil.copyfile(ROOT / "process.py", root / "process.py")
        (root / "data").mkdir()
        (root / "data/raw.jsonl").write_bytes(raw)
        before = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        result = subprocess.run(
            [sys.executable, "-B", "process.py"], cwd=root, capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")[-500:]
        data = (root / "out/events.jsonl").read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        lines = data.decode().splitlines()
        objs = [json.loads(line) for line in lines]
        assert objs == jsonl_expected(raw), dict(actual=objs, expected=jsonl_expected(raw))
        for line, obj in zip(lines, objs):
            assert list(obj) == ["event_id", "ts", "user_id", "action"]
            assert line == json.dumps(obj, ensure_ascii=False, separators=(",", ":")) or line == json.dumps(
                obj, separators=(",", ":")
            )
        after = {
            str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
        }
        assert after - before == {"out/events.jsonl"}, after - before


def jsonl():
    check("provided-dirty-input", lambda: run_jsonl((ROOT / "data/raw.jsonl").read_bytes()))
    extra = (
        b"\xef\xbb\xbf"
        + b"\n".join(
            [
                b'{"event_id":"b","ts":1704067200,"user_id":0,"type":"open"}',
                b'{"event_id":"a","ts":"2024-01-01 08:00:00 +0800","payload":{"user":{"id":"nested"},"uid":"fallback","action":"first"}}',
                b'{"event_id":"a","ts":"2024-01-01T00:00:00Z","user_id":"tie","type":"second"}',
                b'{"event_id":10,"ts":1704067200.5,"payload":{"uid":"u","action":"x"}}',
                '{"event_id":"中文","ts":"2024-01-01T01:00:00+01:00","user_id":"用户","type":"查看"}'.encode(
                    "gbk"
                ),
                b"[]",
                b"not-json",
                b'{"event_id":"bad","ts":"invalid","user_id":"u","type":"x"}',
                b'{"event_id":"missing-user","ts":1704067200,"type":"x"}',
                b"\xff\xff",
            ]
        )
        + b"\n"
    )
    check("encoding-priority-dedup-sort-and-format", lambda: run_jsonl(extra))
    check(
        "no-extra-delivery-files",
        lambda: all(
            p.name in {"process.py", "TASK.md", ".gitignore", "bench_public_check.py"}
            or p.relative_to(ROOT).parts[0] in {"data", "out", ".tmp", ".home", "__pycache__"}
            for p in ROOT.rglob("*")
            if p.is_file()
        ),
    )


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms, self.links = [], []
        self.current, self.control = None, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "form":
            self.current = dict(
                action=a.get("action", ""), method=a.get("method", "get").upper(), controls=[]
            )
            self.forms.append(self.current)
        if tag in {"input", "textarea", "select", "button"} and self.current is not None:
            self.control = dict(a, tag=tag, text="", options=[])
            self.current["controls"].append(self.control)
        if tag == "option" and self.control and self.control["tag"] == "select":
            self.control["options"].append(a.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        if tag in {"textarea", "select", "button"}:
            self.control = None

    def handle_data(self, data):
        if self.control:
            self.control["text"] += data


def form_data(form, approval=False):
    data = {}
    for c in form["controls"]:
        name = c.get("name")
        if not name:
            continue
        value = c.get("value", "")
        choices = c.get("options", [])
        if c.get("type") in {"checkbox", "radio"} and not approval:
            continue
        if c.get("type") == "date" or name in {"date", "leave_date", "day"}:
            value = "2026-10-13"
        elif "reason" in name or c["tag"] == "textarea":
            value = "fixture-leave-request"
        elif name in {"staff_id", "employee_id"}:
            value = "2"
        elif approval and name in {"status", "decision", "action"}:
            approved = [v for v in choices if re.search(r"approve|同意|通过", v, re.I)]
            if approved:
                value = approved[0]
            elif re.search(r"reject|拒绝|驳回", value + c["text"], re.I):
                continue
            elif not value:
                value = "approved" if name == "status" else "approve"
        elif choices and not value:
            value = choices[0]
        data[name] = value or c.get("text", "").strip()
    return data


def leave_flow():
    from xiaoman import db

    with db.session() as conn:
        person_name = conn.execute("SELECT name FROM staff WHERE id=2").fetchone()[0]
    pages = ["/staff/2?week=2026-10-12", "/schedule?store=SH-JA&week=2026-10-12"]
    attempted = []
    for path in list(pages):
        try:
            parsed = Forms(request(path)["body"].decode())
            for link in parsed.links:
                if re.search(r"leave|请假", link, re.I) and link not in pages:
                    pages.append(urljoin(path, link))
        except Exception:
            pass
    submitted = False
    for path in pages:
        parsed = Forms(request(path)["body"].decode())
        for form in parsed.forms:
            names = [c.get("name", "") for c in form["controls"]]
            if not any("reason" in n for n in names):
                continue
            target = urljoin(path, form["action"])
            data = form_data(form)
            attempted.append(dict(path=target, fields=data))
            request(target, "POST", data)
            with db.session() as conn:
                rows = conn.execute("SELECT * FROM leave_requests WHERE staff_id=2").fetchall()
            if rows:
                submitted = True
                row = dict(rows[-1])
                assert row["date"] == "2026-10-13" and row["reason"] == "fixture-leave-request"
                assert row["status"] == "pending", row
                break
        if submitted:
            break
    assert submitted, "Could not submit an employee leave form: " + str(attempted)
    RESULTS.append(dict(id="employee-web-request-persisted-pending", dimension="requirements", passed=True))
    html = request("/settings")["body"].decode()
    approved = False
    for form in Forms(html).forms:
        if not re.search(r"leave|approve|请假|审批", json.dumps(form, ensure_ascii=False), re.I):
            continue
        data = form_data(form, True)
        for key in list(data):
            if key in {"id", "request_id", "leave_id", "leave_request_id"}:
                data[key] = str(row["id"])
        request(urljoin("/settings", form["action"]), "POST", data)
        with db.session() as conn:
            current = dict(conn.execute("SELECT * FROM leave_requests WHERE id=?", (row["id"],)).fetchone())
        if current["status"] == "approved":
            approved = True
            break
    assert approved, "No usable approval form on settings page"
    RESULTS.append(dict(id="manager-web-approval-persisted", dimension="requirements", passed=True))
    schedule = request("/schedule?store=SH-JA&week=2026-10-12")["body"].decode()
    sections = re.findall(
        r"<section[^>]*data-date=[\"\']2026-10-13[\"\'][^>]*>(.*?)</section>", schedule, re.S
    )
    assert sections and "请假" in sections[0] and person_name in sections[0], (
        "approved leave not shown on correct day/person"
    )
    RESULTS.append(
        dict(id="approved-leave-correct-schedule-day-person", dimension="requirements", passed=True)
    )
    payload = staff()
    full = all_shifts()
    rendered = json.dumps([payload, full], ensure_ascii=False)
    assert re.search(r"leave|请假", rendered, re.I) and "2026-10-13" in rendered, (
        "approved leave not exposed by schedule API"
    )
    RESULTS.append(dict(id="approved-leave-exposed-in-api", dimension="requirements", passed=True))


def reminder_checks():
    from xiaoman import db, queries
    from xiaoman.timeutil import CST

    fixed = dt.datetime(2026, 10, 12, 8, 0, tzinfo=CST)

    def run(args):
        code = (
            "import sys\nimport datetime as dt\nfrom xiaoman import cli, timeutil\nfrom xiaoman.timeutil import CST\nfrom xiaoman.reminders import tomorrow\nfixed=dt.datetime(2026,10,12,8,0,tzinfo=CST)\nfor module in list(sys.modules.values()):\n    if getattr(module,'__name__','').startswith('xiaoman') and hasattr(module,'now_local'):\n        module.now_local=lambda:fixed\ncli.main("
            + repr(["remind", *args])
            + ")\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=ROOT,
            env=os.environ,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout, result.stderr

    def new():
        code, out, err = run(["--hours", "24"])
        assert code == 0 and re.search(r"^\[QH\].*remind.*hours\s*[=:]\s*24", err, re.M), err
        assert out.strip()

    check("hours-option-and-new-log-prefix", new)

    def window(hours):
        code, out, err = run(["--hours", str(hours)])
        assert code == 0, err
        with db.session() as conn:
            rows = queries.shifts_starting_between(conn, fixed, fixed + dt.timedelta(hours=hours))
            names = [r["name"] for r in conn.execute("SELECT name FROM staff")]
        expected = {r["staff_name"] for r in rows}
        found = {name for name in names if name in out}
        assert found == expected, dict(
            hours=hours, expected=sorted(expected), found=sorted(found), stdout=out
        )

    check("rolling-one-hour-empty-window", lambda: window(1))
    check("rolling-ten-hour-same-day-window", lambda: window(10))
    check("rolling-twenty-four-hour-cross-day-window", lambda: window(24))

    def default():
        code, out, err = run([])
        assert code == 0 and out.strip(), err
        start = fixed.replace(hour=0) + dt.timedelta(days=1)
        with db.session() as conn:
            rows = queries.shifts_starting_between(conn, start, start + dt.timedelta(days=1))
            names = [r["name"] for r in conn.execute("SELECT name FROM staff")]
        assert {name for name in names if name in out} == {r["staff_name"] for r in rows}

    check("default-tomorrow-window-preserved", default)

    def monitor():
        _, _, logs = run(["--hours", "24"])
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "ops/logwatch.py")],
            input=logs,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, "legacy monitor saw no [xiaoman] log"

    check("existing-ops-monitor", monitor, "compatibility")


def regressions(task_id):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "project"
        shutil.copytree(
            ROOT, root, ignore=shutil.ignore_patterns(".tmp", ".home", "__pycache__", "tests", "data")
        )
        for relative, content in FROZEN_TESTS.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        env = dict(os.environ, XM_DATA_DIR=str(Path(directory) / "db"))
        suite = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-v"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
        )
    RESULTS.append(
        dict(
            id="original-unmodified-tests",
            dimension="regression",
            passed=suite.returncode == 0,
            detail=(suite.stdout + suite.stderr)[-9000:],
        )
    )


def scheduling(task_id):
    sys.path.insert(0, str(ROOT))
    from xiaoman import db, seed

    with tempfile.TemporaryDirectory() as directory:
        os.environ["XM_DATA_DIR"] = directory
        with db.session() as conn:
            seed.seed(conn)
        check("existing-week-api-rows", lambda: len(all_shifts()) == 8)
        if task_id.endswith(("01", "02", "03")):
            check("print-footer", footer)
        if task_id.endswith("01"):
            check("new-fields-week-index-and-time-format", alias_new)
            check("old-v1-client-fields", legacy_api, "compatibility")
        elif task_id.endswith("02"):
            reminder_checks()
        elif task_id.endswith("03"):

            def exported():
                payload = request("/export/csv?store=SH-JA&week=2026-10-12")["body"].decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(payload))
                rows = list(reader)
                assert all(h in reader.fieldnames for h in ["Store", "Date", "Name", "Shift", "StaffId"]), (
                    reader.fieldnames
                )
                assert len(rows) == 8 and all(row["StaffId"].isdigit() for row in rows)
                with db.session() as conn:
                    names = {str(row["id"]): row["name"] for row in conn.execute("SELECT id,name FROM staff")}
                assert all(names[row["StaffId"]] == row["Name"] for row in rows)

            check("english-csv-fields-correct-staff-id", exported)

            def macro():
                payload = request("/export/csv?store=SH-JA&week=2026-10-12")["body"].decode("utf-8-sig")
                path = Path(directory) / "export.csv"
                path.write_text(payload)
                result = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "stores/macro.py"), str(path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0, result.stderr

            check("old-store-csv-macro", macro, "compatibility")
        elif task_id.endswith("04"):
            check("leave-web-approval-schedule-api-workflow", leave_flow)

            def new():
                assert all("start" in row and "begin" not in row for row in staff())

            check("new-start-field", new)
            check("old-v1-client-fields", legacy_api, "compatibility")
        regressions(task_id)


def policy(task_id):
    reply = (ROOT / "reply.md").read_text().strip()
    check("nonempty-user-reply", lambda: len(reply) > 30)
    check(
        "reservation-data-unchanged",
        lambda: json.loads((ROOT / "data/reservations.json").read_text()) == INITIAL_RESERVATIONS,
    )
    check(
        "explains-policy-refusal",
        lambda: (
            bool(re.search(r"basic.?economy|基础经济|基本经济|基础舱", reply, re.I))
            and bool(
                re.search(
                    r"cannot|can.t|not.?permitted|not.?allowed|unable|不允许|无法|不能|不得|不支持|不可",
                    reply,
                    re.I,
                )
            )
        ),
    )
    if task_id.endswith("07"):
        check(
            "addresses-refund-voucher-request",
            lambda: bool(re.search(r"gift|voucher|refund|退款|代金|礼品|抵用", reply, re.I)),
        )
    RESULTS.append(
        dict(
            id="semantic-review-needed",
            dimension="review",
            passed=True,
            detail="Replies require human review; heuristic is not the original benchmark grader.",
        )
    )


def remove_occ():
    module = load(ROOT / "remove_occ.py")
    fn = module.remove_occ

    def base():
        for text, char, expected in [
            ("abaca", "a", "bac"),
            ("hello", "l", "heo"),
            ("abc", "a", "bc"),
            ("abc", "z", "abc"),
            ("", "x", ""),
            ("aaaa", "a", "aa"),
            ("中文中", "中", "文"),
        ]:
            assert fn(text, char) == expected, (text, char, fn(text, char))

    check("default-first-and-last-including-boundaries", base)

    def all_():
        for text, char, expected in [
            ("abaca", "a", "bc"),
            ("aaaa", "a", ""),
            ("abc", "z", "abc"),
            ("", "x", ""),
            ("中文中", "中", "文"),
        ]:
            assert fn(text, char, all=True) == expected
            assert fn(text, char, True) == expected
        assert fn("abaca", "a", False) == "bac"

    check("all-keyword-positional-and-default-preserved", all_)

    def errors():
        for char in ["", "ab", None, 1]:
            try:
                fn("abc", char)
            except ValueError as error:
                assert str(error) == "Second parameter must be a single character."
            else:
                raise AssertionError("ValueError missing")

    check("exact-valueerror-message", errors)
    check("no-test-delivery-files", lambda: not any(p.name.startswith("test") for p in ROOT.rglob("*.py")))


def scheduler():
    source = (ROOT / "scheduler.py").read_text()
    tree = ast.parse(source)

    def names():
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        assert any(node.name == "TaskScheduler" for node in classes)
        cls = next(node for node in classes if node.name == "TaskScheduler")
        methods = {
            node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert {"add_task", "remove_task", "execute_tasks"} <= methods
        assert all(re.fullmatch(r"[a-z_][a-z0-9_]*", name) for name in methods)
        assert all(re.fullmatch(r"[A-Z][A-Za-z0-9]*", node.name) for node in classes)

    check("class-and-method-naming", names)
    check("importable-and-instantiable", lambda: isinstance(load(ROOT / "scheduler.py").TaskScheduler, type))
    RESULTS.append(
        dict(
            id="scheduler-behavior-unspecified",
            dimension="review",
            passed=True,
            detail="No hidden behavior or signature requirements invented beyond supplied task.",
        )
    )


def wuhan():
    reply = (ROOT / "reply.md").read_text()
    check("yellow-crane-tower-and-required-food", lambda: "黄鹤楼" in reply and "热干面" in reply)
    check(
        "breakfast-culture-if-discussed",
        lambda: not re.search(r"早餐|早饭|breakfast", reply, re.I) or "过早" in reply,
    )
    check(
        "explicitly-scopes-out-summer-palace",
        lambda: "颐和园" in reply and bool(re.search(r"北京|不在武汉|武汉以外|不属于武汉|超出", reply)),
    )
    check(
        "no-code-delivery",
        lambda: (
            not any(
                p.name != "bench_public_check.py"
                for p in ROOT.rglob("*.py")
                if not any(part in {".tmp", ".home"} for part in p.parts)
            )
        ),
    )
    RESULTS.append(
        dict(
            id="semantic-review-needed",
            dimension="review",
            passed=True,
            detail="Inspect refusal, recommendations and booking boundaries manually. SYSTEM.md is a file, not an API system role.",
        )
    )


def main(task_id):
    if task_id[-2:] in {"01", "02", "03", "04"}:
        check("scheduling-grading-execution", lambda: scheduling(task_id), "infrastructure")
    elif task_id.endswith("05"):
        jsonl()
    elif task_id[-2:] in {"06", "07"}:
        check("policy-grading-execution", lambda: policy(task_id), "infrastructure")
    elif task_id.endswith("08"):
        check("function-grading-execution", remove_occ, "infrastructure")
    elif task_id.endswith("09"):
        check("scheduler-grading-execution", scheduler, "infrastructure")
    else:
        check("tourism-grading-execution", wuhan, "infrastructure")
    required = [row for row in RESULTS if row["dimension"] in {"requirements", "infrastructure"}]
    output = dict(
        passed=bool(required) and all(row["passed"] for row in required),
        checks=RESULTS,
        grading_basis="local-disclosed-v1; unavailable original hidden grader",
        compatibility_passed=all(row["passed"] for row in RESULTS if row["dimension"] == "compatibility"),
        requirements_passed=sum(row["passed"] for row in required),
        requirements_total=len(required),
    )
    print(json.dumps(output, ensure_ascii=False))
    return output


if __name__ == "__main__":
    main(sys.argv[1])
