"""Splitting text into Telegram-sized messages, and Claude's markdown to Telegram HTML."""
import html
import re


def chunks(text, limit=4096):
    """Split on paragraph, then line boundaries so each part fits Telegram's limit of 4096 characters."""
    parts, text = [], text.strip()
    while text:
        cut = len(text)
        if cut > limit:
            cut = next((i for i in (text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit)) if i > limit // 2), limit)
        if part := text[:cut].rstrip():
            parts.append(part)
        text = text[cut:].lstrip("\n")
    return parts


FENCE_RE = re.compile(r"^\s*(```[\w+#.-]*)")


def md_chunks(text, limit=4000):
    """chunks() for markdown: a part that ends inside a ``` block closes it, and the next part reopens it, so every part converts on its own. The limit leaves room for the added fences; markup and escapes do not count towards Telegram's 4096, only visible text."""
    parts, carry = [], None
    for part in chunks(text, limit):
        if carry:
            part = carry + "\n" + part
        open_ = None
        for line in part.split("\n"):
            if m := FENCE_RE.match(line):
                open_ = None if open_ else m.group(1)
        parts.append(part + "\n```" if open_ else part)
        carry = open_
    return parts


def md_inline(s):
    """Claude's inline markdown to Telegram HTML: code first (its content is never formatted), then escape, links, bold, strikethrough, italics. snake_case and 2*3 stay as they are."""
    codes = []

    def keep(m):
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"
    s = html.escape(re.sub(r"`([^`\n]+)`", keep, s.replace("\x00", "")), quote=False)  # a NUL in Claude's text would read as a placeholder
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", s)
    s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s)
    s = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)", r"<i>\1</i>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def md_html(text):
    """Claude's markdown to the HTML subset Telegram renders (parse_mode HTML): headings bold, bullets as •, ``` blocks and tables as monospace blocks, quotes marked with ▎."""
    out, code, lang, table = [], None, "", []

    def pre(lines, cls=""):
        return f"<pre><code{cls}>{html.escape(chr(10).join(lines), quote=False)}</code></pre>"

    def flush_table():
        if table:
            out.append(pre([t for t in table if not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", t)]))
            table.clear()
    for line in text.split("\n"):
        if code is not None:
            if FENCE_RE.match(line):
                out.append(pre(code, lang))
                code = None
            else:
                code.append(line)
            continue
        if m := FENCE_RE.match(line):
            flush_table()
            code, lang = [], f' class="language-{m.group(1)[3:]}"' if len(m.group(1)) > 3 else ""
            continue
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            table.append(line.strip())
            continue
        flush_table()
        if h := re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line):
            out.append(f"<b>{md_inline(h.group(1))}</b>")
        elif re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            out.append("──────────")
        elif b := re.match(r"^(\s*)[-*+]\s+(.*)$", line):
            out.append(f"{b.group(1)}• {md_inline(b.group(2))}")
        elif q := re.match(r"^\s*>\s?(.*)$", line):
            out.append(f"▎ {md_inline(q.group(1))}")
        else:
            out.append(md_inline(line))
    if code is not None:
        out.append(pre(code, lang))
    flush_table()
    return "\n".join(out)
