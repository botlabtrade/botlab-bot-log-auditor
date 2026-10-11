"""Tiny dependency-free PDF writer for the Pro audit export (text only, Helvetica).

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import re
import textwrap

PAGE_W, PAGE_H = 595, 842  # A4 points
MARGIN = 50
LEADING = 13
WRAP = 98

_REPL = {"·": "-", "—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
         "≥": ">=", "≤": "<=", "∞": "inf", "−": "-", "…": "..."}


def _ascii(s: str) -> str:
    for k, v in _REPL.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def markdown_to_lines(md: str):
    """-> list of (text, font_size, bold)."""
    out = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if re.match(r"^\|?\s*-{3,}", line):
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            out.append(("", 10, False))
            out.append((m.group(2).replace("**", ""), 14 if len(m.group(1)) == 1 else 12, True))
            continue
        line = line.replace("**", "")
        if line.startswith("_") and line.endswith("_"):
            line = line.strip("_")
        if line.startswith("|"):
            line = "  ".join(c.strip() for c in line.strip("|").split("|"))
        indent = "    " if line.startswith(("- ", "* ")) else ""
        if indent:
            line = "- " + line[2:]
        for i, w in enumerate(textwrap.wrap(line, WRAP) or [""]):
            out.append(((indent if i else "") + w, 10, False))
    return out


def build_pdf(title: str, markdown: str, footer: str = "") -> bytes:
    lines = [(title, 16, True), ("", 10, False)] + markdown_to_lines(markdown)
    pages, cur, y = [], [], PAGE_H - MARGIN
    for text, size, bold in lines:
        step = LEADING + (size - 10)
        if y - step < MARGIN + 20:
            pages.append(cur)
            cur, y = [], PAGE_H - MARGIN
        y -= step
        cur.append((text, size, bold, y))
    pages.append(cur)

    objs = []  # index i -> object number i+1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_b = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    pages_num = len(objs) + 1
    objs.append(b"")  # placeholder for Pages
    kids = []
    for pi, page in enumerate(pages, 1):
        ops = []
        for text, size, bold, y in page:
            if text:
                ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {MARGIN} {y} Td ({_esc(_ascii(text))}) Tj ET")
        foot = f"{_ascii(footer)}   page {pi}/{len(pages)}"
        ops.append(f"BT /F1 8 Tf {MARGIN} 30 Td ({_esc(foot)}) Tj ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Resources << /Font << /F1 {font} 0 R /F2 {font_b} 0 R >> >> /Contents {content} 0 R >>".encode()))
    objs[pages_num - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    catalog = add(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
