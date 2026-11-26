import base64
import copy
import hashlib
import json
import re
from .files import read_file
from .provider_catalog import is_account_provider
LIMIT = 8 * 1024 * 1024
MARKER = "\n[GoalKeeper 图片附件]"

def expand_request(request, root):
    items = []
    for item in request.input:
        content = item.get('content')
        if item.get('role') != 'user' or not isinstance(content, str) or MARKER not in content:
            items.append(item)
            continue
        text, raw_manifest = content.rsplit(MARKER, 1)
        try:
            manifest = json.loads(raw_manifest)
        except ValueError:
            items.append(item)
            continue
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 4:
            items.append(item)
            continue
        parts = [{'type': 'input_text', 'text': text}]
        total = 0
        for image in manifest:
            path = image.get('path', '')
            if not re.fullmatch(r'attachments/image-[0-9a-f]{32}\.png', path):
                raise ValueError('图片附件无效。')
            raw = read_file(root, path, LIMIT)
            if hashlib.sha256(raw).hexdigest() != image.get('sha256'):
                raise ValueError('图片附件已变化，未将更改后的图片发送给模型。')
            total += len(raw)
            parts.append({'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(raw).decode()})
        if total > 16 * 1024 * 1024:
            raise ValueError('图片总大小超过 16 MB。')
        items.append({**item, 'content': parts})
    request.input = items
    return request


class ImageProvider:
    """Expand after the engine measures textual context, before the HTTP request.

    Base64 is a transport encoding, not hundreds of thousands of text tokens.
    Immutable hashes retained in the request archive identify the exact pixels.
    Actual billed usage still comes from the provider's response.
    """
    def __init__(self, provider, root):
        self.provider, self.root = provider, root

    async def stream(self, request):
        wire = expand_request(copy.deepcopy(request), self.root)
        if not is_account_provider(wire.config.provider_id) and any(
                isinstance(item.get('content'), list) and any(part.get('type') == 'input_image'
                for part in item['content']) for item in wire.input):
            raise ValueError('此会话含图片，当前第三方模型接入尚未支持图片，请使用 GoalKeeper 账号。')
        async for event in self.provider.stream(wire):
            yield event

    async def close(self):
        if hasattr(self.provider, 'close'):
            await self.provider.close()
