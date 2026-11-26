"""Authorized bytes and sandboxed Office rendering; never invokes a model."""
import asyncio
import base64
import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

from ..paths import PROJECT
from ..sandbox import runtime_roots
from .files import read_file
from .permissions import DesktopScope, PreviewSeatbelt

LIMIT = 20_000_000
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}


async def preview_bytes(root, name, office):
    extension = Path(name).suffix.lower()
    if extension not in IMAGE_TYPES | {'.pdf', '.docx'}:
        raise ValueError('此格式不支持页面预览。')
    # Every read revalidates workspace and immutable delivery identity before cache lookup.
    data = read_file(root, name, LIMIT)
    notice = None
    if extension == '.docx':
        data, fonts = await office.render(data)
        if fonts:
            notice = '预览已替换未安装的字体：' + '、'.join(fonts[:8]) + '。'
    return {'type': 'image-bytes' if extension in IMAGE_TYPES else 'pdf',
            'data': base64.b64encode(data).decode(), **({'notice': notice} if notice else {})}


class OfficePreview:
    """One foreground conversion at a time, bounded memory cache by actual source bytes."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.slot = asyncio.Semaphore(1)
        self.cache = {}

    async def render(self, source):
        key = hashlib.sha256(source).hexdigest()
        async with self.slot:
            if key in self.cache:
                return self.cache[key]
            script = PROJECT / 'desktop/office/preview.mjs'
            modules = PROJECT / 'desktop/node_modules'
            node = shutil.which('node')
            if not node or not script.is_file() or not (modules / '@deepseek-ai/libreoffice-kit').exists():
                raise ValueError('Office 预览运行时不可用，请在本地应用中打开文件。')
            directory = self.directory / uuid4().hex
            directory.mkdir(parents=True, mode=0o700)
            scope = DesktopScope(directory, writable=False, runtimes=runtime_roots(),
                                 extra_read=[modules.resolve(), script, Path('/Library/Fonts')])
            runner = PreviewSeatbelt(scope)
            runner.archive = directory
            original = runner.temp / 'source.docx'
            rendered = runner.temp / 'preview.pdf'
            metadata = runner.temp / 'metadata.json'
            original.write_bytes(source)
            try:
                result = await runner.run([node, str(script), str(original), str(rendered), str(metadata)], 60)
                if result['exit_code'] != 0 or not rendered.is_file():
                    raise ValueError('Word 页面转换失败，请用本地应用打开文件。')
                data = read_file(runner.temp, rendered.name, LIMIT)
                if not data.startswith(b'%PDF-'):
                    raise ValueError('Word 预览未生成有效 PDF。')
                fonts = json.loads(metadata.read_text()).get('missingFonts', [])
                if not isinstance(fonts, list) or any(not isinstance(font, str) for font in fonts):
                    raise ValueError('Office 预览元数据无效。')
                while self.cache and (len(self.cache) >= 8 or sum(len(v[0]) for v in self.cache.values()) + len(data) > 64_000_000):
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = (data, fonts)
                return data, fonts
            finally:
                await runner.stop()
                shutil.rmtree(directory, ignore_errors=True)
