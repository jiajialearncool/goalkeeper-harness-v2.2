"""Preview only the top-level delivery answer. Never execute partial arguments."""
import json


def answer_prefix(raw):
    decoder = json.JSONDecoder()
    pos = 0
    try:
        pos = len(raw) - len(raw.lstrip())
        if raw[pos:pos + 1] != '{':
            return ''
        pos += 1
        while True:
            while raw[pos:pos + 1] and raw[pos] in ' \t\r\n,':
                pos += 1
            key, pos = decoder.raw_decode(raw, pos)
            if not isinstance(key, str):
                return ''
            while raw[pos:pos + 1] and raw[pos].isspace():
                pos += 1
            if raw[pos:pos + 1] != ':':
                return ''
            pos += 1
            while raw[pos:pos + 1] and raw[pos].isspace():
                pos += 1
            if key != 'answer':
                _, pos = decoder.raw_decode(raw, pos)
                continue
            if raw[pos:pos + 1] != '"':
                return ''
            start, pos = pos, pos + 1
            # Find the longest complete JSON string prefix. Incomplete escapes
            # and UTF-16 surrogate pairs wait for the following chunk.
            end = pos
            while pos < len(raw):
                if raw[pos] == '"':
                    return decoder.raw_decode(raw, start)[0]
                if raw[pos] == '\\':
                    if pos + 1 >= len(raw):
                        break
                    if raw[pos + 1] == 'u':
                        if pos + 6 > len(raw):
                            break
                        code = int(raw[pos + 2:pos + 6], 16)
                        if 0xD800 <= code <= 0xDBFF:
                            if pos + 12 > len(raw) or raw[pos + 6:pos + 8] != '\\u':
                                break
                            if not 0xDC00 <= int(raw[pos + 8:pos + 12], 16) <= 0xDFFF:
                                return ''
                            pos += 6
                        elif 0xDC00 <= code <= 0xDFFF:
                            return ''
                        pos += 6
                    else:
                        pos += 2
                else:
                    pos += 1
                end = pos
            return json.loads(raw[start:end] + '"')
    except (ValueError, IndexError):
        return ''


class DeliveryAnswerStream:
    def __init__(self):
        self.items = {}

    def observe(self, event):
        data = event.data
        if event.type == 'response.output_item.added':
            item = data.get('item', {})
            if item.get('type') == 'function_call' and item.get('name') == 'request_delivery' and item.get('id'):
                self.items[item['id']] = {'raw': item.get('arguments', ''), 'visible': ''}
        elif event.type == 'response.function_call_arguments.delta':
            item = self.items.get(data.get('item_id'))
            if item is not None and isinstance(data.get('delta'), str):
                item['raw'] += data['delta']
                if len(item['raw']) > 1_000_000:
                    self.items.pop(data['item_id'])
                    return ''
                prefix = answer_prefix(item['raw'])
                if prefix.startswith(item['visible']):
                    delta = prefix[len(item['visible']):]
                    item['visible'] = prefix
                    return delta
        return ''
