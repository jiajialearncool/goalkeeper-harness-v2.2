"""Runs in the task sandbox, never in the credential-owning UI service."""
import json
import sys
from pathlib import Path


def inspect_document(path):
    path = Path(path)
    extension = path.suffix.lower()
    if extension == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('PDF 已加密，请提供可读取的文件。')
        pages = [{'page': i + 1, 'text': p.extract_text() or ''} for i, p in enumerate(reader.pages)]
        if not any(p['text'].strip() for p in pages):
            raise ValueError('没有可提取文字；第一版不支持扫描件 OCR。')
        return {'type': 'pdf', 'pages': pages, 'text': '\n'.join(p['text'] for p in pages)}
    if extension == '.docx':
        from docx import Document
        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs]
        tables = [[[c.text for c in r.cells] for r in t.rows] for t in doc.tables]
        return {'type': 'docx', 'paragraphs': paragraphs, 'tables': tables,
                'text': '\n'.join(paragraphs) + '\n' + '\n'.join('\t'.join(r) for t in tables for r in t),
                'notice': '内容预览，不还原完整排版。'}
    if extension == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            sheets = [{'name': sheet.title,
                       'rows': [[str(c) if c is not None else '' for c in row]
                                for row in sheet.iter_rows(values_only=True)]} for sheet in book]
        finally:
            book.close()
        return {'type': 'xlsx', 'sheets': sheets, 'notice': '公式未由电子表格引擎重新计算。',
                'text': '\n'.join(s['name'] + '\n' + '\n'.join('\t'.join(r) for r in s['rows']) for s in sheets)}
    if extension == '.csv':
        import csv
        text = path.read_text(encoding='utf-8-sig')
        return {'type': 'csv', 'rows': list(csv.reader(text.splitlines())), 'text': text}
    text = path.read_text(encoding='utf-8-sig')
    return {'type': 'text', 'text': text}


if __name__ == '__main__':
    try:
        print(json.dumps(inspect_document(sys.argv[1]), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(2)
