"""ChatGPT web UI: HTML -> markdown that keeps code-block language labels.

ChatGPT's "Copy" button returns fences WITHOUT the label shown in the UI
(```tool becomes a bare ```), and uses CRLF line endings. Two helpers:

  chatgpt_html_to_markdown(html)            DOM -> markdown; fences carry the label.
  restore_fence_languages(copy_md, dom_md)  put the DOM labels back into the Copy text
                                            (also normalises line endings to \\n).

Both are optional: protocol/parse.py recognises an unlabelled fence whose body is a
tool call. These make logs, history and other consumers see the real label.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_OPEN = re.compile(r"^ {0,3}(`{3,})(.*)$")
_CLOSE = re.compile(r"^ {0,3}(`{3,})[ \t]*$")
_PLACEHOLDER = "\u00a7\u00a7CODEBLOCK{}\u00a7\u00a7"


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fences(text: str) -> list[tuple[int, str, str, str]]:
    """Closed fenced blocks as (opening line index, marker, lang, body)."""
    lines = normalize_newlines(text).split("\n")
    out: list[tuple[int, str, str, str]] = []
    i = 0
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if not m or "`" in m.group(2):
            i += 1
            continue
        marker = m.group(1)
        info = m.group(2).strip().split()
        j = i + 1
        while j < len(lines):
            c = _CLOSE.match(lines[j])
            if c and len(c.group(1)) >= len(marker):
                break
            j += 1
        if j >= len(lines):  # not closed
            break
        out.append((i, marker, info[0].lower() if info else "", "\n".join(lines[i + 1 : j])))
        i = j + 1
    return out


def _longest_backtick_run(text: str) -> int:
    return max((len(r) for r in re.findall(r"`+", text)), default=0)


def _block_language(block) -> str:
    """Label from the block header ("tool", "python", ...), else a language-* class."""
    label = block.select_one('[data-markdown-copy="exclude"] .truncate')
    if label:
        lang = label.get_text(strip=True).lower()
        if lang and " " not in lang:
            return lang
    code = block.select_one("code")
    if code:
        for cls in code.get("class", []):
            if cls.startswith("language-"):
                return cls[len("language-"):].lower()
    return ""


def chatgpt_html_to_markdown(html: str) -> str:
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one('[data-markdown-text-style="assistant-message"]') or soup

    # 1. Code blocks first: read label + raw text, park them behind placeholders.
    fenced: list[str] = []
    for block in root.select('[data-code-block="true"]'):
        lang = _block_language(block)
        code = block.select_one("[data-code-block-source] code") or block.select_one("code")
        text = normalize_newlines(code.get_text() if code else block.get_text()).strip("\n")
        fence = "`" * max(3, _longest_backtick_run(text) + 1)
        fenced.append(f"{fence}{lang}\n{text}\n{fence}")
        holder = block.find_parent(attrs={"data-client-defined-widget": "code_block"}) or block
        marker = soup.new_tag("p")
        marker.string = _PLACEHOLDER.format(len(fenced) - 1)
        holder.replace_with(marker)

    # 2. Drop UI-only nodes, convert the rest.
    for node in root.select('[data-markdown-copy="exclude"], button, svg, style, script, [hidden]'):
        node.decompose()
    try:
        md = markdownify(str(root), heading_style="ATX", bullets="-",
                         escape_underscores=False, escape_asterisks=False, escape_misc=False)
    except TypeError:  # older markdownify without the escape_* options
        md = markdownify(str(root), heading_style="ATX", bullets="-")

    # 3. Put the fenced blocks back.
    for i, block_md in enumerate(fenced):
        md = md.replace(_PLACEHOLDER.format(i), block_md)
    return re.sub(r"\n{3,}", "\n\n", normalize_newlines(md)).strip()


def _norm_body(body: str) -> str:
    return "\n".join(line.rstrip() for line in body.splitlines()).strip()


def restore_fence_languages(copy_md: str, dom_md: str) -> str:
    """Give bare fences in the Copy text the label of the DOM block with the same body.
    Already-labelled fences and fences without a match are left alone."""
    lines = normalize_newlines(copy_md).split("\n")
    dom = [(lang, _norm_body(body)) for _i, _m, lang, body in _fences(dom_md) if lang]
    used: set[int] = set()
    for start, marker, lang, body in _fences(copy_md):
        if lang:
            continue
        nb = _norm_body(body)
        for idx, (dlang, dbody) in enumerate(dom):
            if idx not in used and dbody == nb:
                used.add(idx)
                indent = re.match(r"^ {0,3}", lines[start]).group(0)
                lines[start] = f"{indent}{marker}{dlang}"
                break
    return "\n".join(lines)