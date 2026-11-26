"""Upgrade an old checkpoint into a linked record, preserving the original bytes."""
import json
import shutil
import time
from pathlib import Path
from uuid import uuid4

from ..paths import inside_project
from ..storage import Store


def load_for_resume(store):
    raw = json.loads(store.path.read_text())
    state = store.load()
    inside_project(Path(state.workspace))
    if raw.get('schema_version', 1) >= 2:
        return state, store
    remaining = state.remaining_seconds
    if remaining is None:
        remaining = max(0, state.deadline-time.time())
    if remaining <= state.config.delivery_save_seconds:
        raise ValueError('原任务剩余时长已用尽，不能通过升级记录重置预算。')
    upgraded = state.model_copy(deep=True)
    upgraded.parent_run_id = state.id
    upgraded.id = uuid4().hex
    upgraded.sequence, upgraded.events = 0, []
    upgraded.deadline = time.time()+remaining
    upgraded.remaining_seconds = remaining
    replacement = Store(store.directory.parent/upgraded.id)
    for archive in store.archives.iterdir():
        if archive.is_file() and not archive.is_symlink():
            shutil.copyfile(archive, replacement.archives/archive.name)
    replacement.save(upgraded, 'legacy_checkpoint_linked', {'parent_run_id': state.id, 'remaining_seconds': remaining})
    return upgraded, replacement
