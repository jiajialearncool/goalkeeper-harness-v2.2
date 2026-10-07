import asyncio
import json
import sys

from goalkeeper.evaluation import parallel


async def test_four_real_offline_processes_overlap_and_keep_cost_files_separate(tmp_path):
    async def job(group):
        root = tmp_path / group
        root.mkdir()
        script = """import json,time,sys
from pathlib import Path
root=Path(sys.argv[1]); group=sys.argv[2]
start=time.time(); (root/'start.json').write_text(json.dumps({'group':group,'time':start}))
time.sleep(.15)
(root/'cost.json').write_text(json.dumps({'group':group,'tokens':100,'attempts':1}))
(root/'end.json').write_text(json.dumps({'group':group,'time':time.time()}))
raise SystemExit(1 if group=='pi' else 0)
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, str(root), group, start_new_session=True
        )
        return await process.wait()

    groups = ["goalkeeper", "codex", "opencode", "pi"]
    result = await parallel([lambda group=group: job(group) for group in groups])
    assert result == [0, 0, 0, 1]
    starts = [json.loads((tmp_path / group / "start.json").read_text())["time"] for group in groups]
    ends = [json.loads((tmp_path / group / "end.json").read_text())["time"] for group in groups]
    assert max(starts) < min(ends)
    for group in groups:
        assert json.loads((tmp_path / group / "cost.json").read_text()) == {
            "group": group,
            "tokens": 100,
            "attempts": 1,
        }
