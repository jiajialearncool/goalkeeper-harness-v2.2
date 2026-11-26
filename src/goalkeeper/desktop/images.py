"""Desktop image attachments. Keep bytes out of public history; expand at request time."""
import base64
import hashlib
import json
import os
import re
import struct
from uuid import uuid4

from .files import safe_file, read_file

from ..runtime.images import ImageProvider, expand_request  # noqa: F401
from ..runtime.provider_catalog import is_account_provider
LIMIT = 8 * 1024 * 1024
MARKER = '\n[GoalKeeper 图片附件]'


def png_bytes(data):
    if not isinstance(data, str) or len(data) > (LIMIT + 2) // 3 * 4:
        raise ValueError('图片超过 8 MB。')
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError as error:
        raise ValueError('图片编码无效。') from error
    if not 33 <= len(raw) <= LIMIT or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR':
        raise ValueError('图片格式无效。')
    w, h = struct.unpack('>II', raw[16:24])
    if not w or not h or w > 8192 or h > 8192 or w * h > 25_000_000:
        raise ValueError('图片尺寸过大。')
    return raw


def attach(manager, wid, cid, data):
    # Reuse the file-reference ownership rules before writing any bytes.
    manager.file_references(cid, wid)
    existing = next((c for c in manager.index['conversations'] if c['id'] == cid), None)
    if existing and existing['workspace_id'] != wid:
        raise ValueError('图片不属于当前工作区。')
    if wid in manager.deleting_workspaces or any(c['workspace_id'] == wid and c['id'] in manager.jobs
                                                for c in manager.index['conversations']):
        raise ValueError('工作区正在执行，请结束任务后添加图片。')
    raw = png_bytes(data)
    name = 'attachments/image-' + uuid4().hex + '.png'
    root = manager.files_root(wid)
    path = safe_file(root, name)
    path.parent.mkdir(exist_ok=True)
    # Refuse links even if the parent changed while creating the directory.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(file, 'wb') as handle:
            handle.write(raw)
    finally:
        os.close(directory)
    return {'id': hashlib.sha256((cid + '\0' + name).encode()).hexdigest(), 'path': name,
            'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'preview': 'data:image/png;base64,' + data}


def add_to_message(manager, cid, wid, text, images):
    if not isinstance(images, list) or len(images) > 4:
        raise ValueError('每条消息最多添加 4 张图片。')
    if not images:
        return text
    selected = manager.index['settings'].get('model_provider', 'account')
    if cid in manager.active_states:
        selected = manager.active_states[cid].config.provider_id
    if not is_account_provider(selected):
        raise ValueError('当前第三方模型接入尚未支持图片，请在输入框选择 GoalKeeper 账号后发送。')
    paths = manager.validate_references(cid, [image['id'] for image in images], wid)
    total = 0
    manifest = []
    for path, image in zip(paths, images):
        if not re.fullmatch(r'attachments/image-[0-9a-f]{32}\.png', path):
            raise ValueError('图片附件无效。')
        raw = read_file(manager.files_root(wid), path, LIMIT)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != image.get('sha256'):
            raise ValueError('图片附件已变化，请重新添加。')
        total += len(raw)
        manifest.append({'path': path, 'sha256': digest})
    if total > 16 * 1024 * 1024:
        raise ValueError('单条消息图片总大小不能超过 16 MB。')
    return (text.strip() or '请查看这些图片。') + MARKER + json.dumps(manifest, ensure_ascii=False)


