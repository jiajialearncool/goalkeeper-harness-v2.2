import http.server
import sys
import io
import secrets
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .files import safe_file
from bs4 import BeautifulSoup


class Handler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        self.style_nonce = None
        path = Path(self.translate_path(self.path))
        if path.suffix.lower() not in {'.html', '.htm'} or not path.is_file():
            return super().send_head()
        soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
        self.style_nonce = secrets.token_urlsafe(24)
        for style in soup.select('style'):
            style['nonce'] = self.style_nonce
        rules = []
        for i, element in enumerate(soup.select('[style]')):
            element['data-gk-preview-style'] = str(i)
            rules.append('[data-gk-preview-style="'+str(i)+'"]{'+element.attrs.pop('style')+'}')
        if rules:
            style = soup.new_tag('style', nonce=self.style_nonce)
            style.string = '\n'.join(rules)
            (soup.head or soup).append(style)
        body = str(soup).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def translate_path(self, path):
        relative = unquote(urlsplit(path).path).lstrip('/') or 'index.html'
        try:
            return str(safe_file(Path(self.directory), relative))
        except ValueError:
            return str(Path(self.directory) / '__not_available__')

    def guess_type(self, path):
        media = super().guess_type(path)
        if media.startswith('text/') or media in {'application/javascript', 'application/json'}:
            return media + '; charset=utf-8'
        return media

    def list_directory(self, path):
        self.send_error(403, 'Directory listing disabled')

    def end_headers(self):
        styles = "style-src 'self' 'nonce-" + self.style_nonce + "'; " if getattr(self, 'style_nonce', None) else ''
        self.send_header('Content-Security-Policy', "default-src 'self' data: blob:; " + styles + "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors goalkeeper://app")
        super().end_headers()


if __name__ == '__main__':
    import functools
    http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])),
                                    functools.partial(Handler, directory=sys.argv[2])).serve_forever()
