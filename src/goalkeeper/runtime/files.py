import hashlib
import os
import stat
import re
import json
from pathlib import Path

SKIP = {'.git', '.home', '.tmp', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
        '.DS_Store', '.ssh', '.aws', '.codex', '.agents', 'credentials'}


def importable(name):
    return permitted(name) and name not in {'runtime', 'outputs'}


def permitted(name):
    return (name not in SKIP and not name.startswith('.env')
            and not any(x in name.lower() for x in ('credential', 'secret', 'token.json', 'account.json'))
            and not name.endswith(('.pem', '.key', '.p12', '.pfx')))


def sensitive(name):
    value = name.lower()
    return (name in {'.ssh', '.aws', '.codex', '.agents', 'credentials'} or name.startswith('.env')
            or any(x in value for x in ('credential', 'secret', 'token.json', 'account.json'))
            or name.endswith(('.pem', '.key', '.p12', '.pfx')))


def workspace_secret_rules(root):
    """Protect credentials already in a directly selected project, including command access."""
    root = Path(root).resolve()
    pattern = (re.escape(str(root)) + '/(.*/)?(\\.env[^/]*|\\.ssh|\\.aws|\\.codex|\\.agents|'
               '[^/]*[Cc][Rr][Ee][Dd][Ee][Nn][Tt][Ii][Aa][Ll][^/]*|[^/]*[Ss][Ee][Cc][Rr][Ee][Tt][^/]*|'
               '[^/]*[Tt][Oo][Kk][Ee][Nn]\\.[Jj][Ss][Oo][Nn][^/]*|[^/]*[Aa][Cc][Cc][Oo][Uu][Nn][Tt]\\.[Jj][Ss][Oo][Nn][^/]*|'
               '[^/]*\\.(pem|key|p12|pfx))($|/.*)')
    rules = [f'(deny file-read-data file-write* (regex {json.dumps(pattern, ensure_ascii=False)}))',
             f'(deny file-write-unlink (literal {json.dumps(str(root), ensure_ascii=False)}))']
    # Prevent moving a parent directory to give an existing protected file a new, unprotected name.
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in [*folders, *files]:
            path = Path(directory) / name
            if sensitive(name):
                rules.append(f'(deny file-read-data file-write* (subpath {json.dumps(str(path), ensure_ascii=False)}))')
                for parent in path.parents:
                    if not parent.is_relative_to(root):
                        break
                    rules.append(f'(deny file-write-unlink (literal {json.dumps(str(parent), ensure_ascii=False)}))')
        folders[:] = [name for name in folders if name not in SKIP and not sensitive(name)
                      and not (Path(directory) / name).is_symlink()]
    return '\n'.join(dict.fromkeys(rules))


def safe_file(root, name):
    path = Path(name)
    if path.is_absolute() or any(p in {'.', '..'} or not permitted(p) for p in path.parts):
        raise ValueError('文件不在允许范围内。')
    if root.is_symlink():
        raise ValueError('工作区目录不能是符号链接。')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('不允许通过符号链接读取文件。')
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('文件不在工作区内。')
    return current


def read_file(root, name, limit=20_000_000):
    safe_file(root, name)
    parts = Path(name).parts
    if not parts:
        raise ValueError('请选择文件。')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(file, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('文件不是普通文件，或超过单次 20MB 预览大小。')
            content = handle.read(limit + 1)
            if len(content) > limit:
                raise ValueError('文件超过单次 20MB 预览大小。')
            return content
    finally:
        os.close(directory)


def workspace_snapshot(root):
    """Freeze regular deliverable files, excluding links and private/cache paths.

    Links may be required while a task is running, but they are never copied into
    the immutable delivery snapshot. A caller that explicitly declares a link as
    a deliverable will reject it because it is absent from this mapping.
    """
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(p for p in folders if permitted(p) and not (Path(directory) / p).is_symlink())
        for name in sorted(files):
            file = Path(directory) / name
            if not permitted(name):
                continue
            if file.is_symlink():
                continue
            result[str(file.relative_to(root))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def workspace_identity(root):
    """Describe live workspace state without following symbolic links.

    This identity invalidates checks after workspace changes. It is deliberately
    separate from ``workspace_snapshot``: a link is valid runtime state even
    though it is not an exportable delivery file.
    """
    root = Path(root)
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        kept = []
        for name in sorted(folders):
            if not permitted(name):
                continue
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if path.is_symlink():
                result[relative] = 'link:' + os.readlink(path)
            else:
                kept.append(name)
        folders[:] = kept
        for name in sorted(files):
            if not permitted(name):
                continue
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if path.is_symlink():
                result[relative] = 'link:' + os.readlink(path)
            else:
                result[relative] = 'file:' + hashlib.sha256(path.read_bytes()).hexdigest()
    return result
