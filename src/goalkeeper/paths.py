from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DATA = PROJECT / "runtime"
PRIVATE = DATA / "private"


def inside_project(path: Path) -> Path:
    result = path.resolve()
    if not result.is_relative_to(PROJECT.resolve()):
        raise ValueError("所有任务、配置和输出必须位于当前项目。")
    return result
