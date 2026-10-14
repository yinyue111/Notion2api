"""Scrub the Notion account owner's identity from model output.

Notion's server-side agent tools (``view`` on threads/users, ``query-data-sources``)
run with the account's token, so a model can still discover the owner's name/email
even when the transcript carries no identity. This is a deterministic last line of
defence applied to every text item streamed back from Notion.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

NAME_REPLACEMENT = "there"
EMAIL_REPLACEMENT = "[redacted]"
_MENTION_USER_RE = re.compile(r"<mention-user\b[^>]*?(?:/>|>.*?</mention-user>)", re.I | re.S)
_USER_URL_RE = re.compile(r"user://[0-9a-f-]{32,36}", re.I)
# Notion-flavoured markup that leaks into answers: mention tags and citation footnotes.
_MENTION_PAGE_RE = re.compile(r"<mention-(?:page|database|data-source|agent)\b[^>]*?(?:/>|>(.*?)</mention-(?:page|database|data-source|agent)>)", re.I | re.S)
_MENTION_DATE_RE = re.compile(r"<mention-date\b[^>]*?\bstart=\"([^\"]*)\"[^>]*?(?:/>|>.*?</mention-date>)", re.I | re.S)
_FOOTNOTE_RE = re.compile(r"\[\^(?:toolu_\w+|https?://[^\]\s]+|[a-z][\w-]*://[^\]\s]+|\{\{[^\]]*\}\})\]")
# Hold-back for a partial user:// URL; unfinished <tags> are held separately.
_TAG_HOLD = 48


class IdentityRedactor:
    def __init__(self, names: Iterable[str] = (), emails: Iterable[str] = ()):
        terms: List[tuple[str, str]] = []
        for e in emails:
            e = (e or "").strip()
            if "@" in e:
                terms.append((e, EMAIL_REPLACEMENT))
                local = e.split("@", 1)[0]
                if len(local) >= 5:
                    terms.append((local, EMAIL_REPLACEMENT))
        for n in names:
            n = (n or "").strip()
            if len(n) >= 2 and n.lower() not in {"user", "notion", "ai", "me", "you"}:
                terms.append((n, NAME_REPLACEMENT))
        # longest first so an email wins over its local part
        terms.sort(key=lambda t: len(t[0]), reverse=True)
        self._terms = terms
        self._term_re = (
            re.compile("|".join(r"(?<![\w.])" + re.escape(t) + r"(?![\w])" for t, _ in terms), re.I)
            if terms else None
        )
        self._repl = {t.lower(): r for t, r in terms}
        self._hold = max([len(t) for t, _ in terms] + [_TAG_HOLD])

    def patterns(self):
        pats = [_MENTION_USER_RE, _USER_URL_RE, _MENTION_PAGE_RE, _MENTION_DATE_RE, _FOOTNOTE_RE]
        if self._term_re is not None:
            pats.append(self._term_re)
        return pats

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = _MENTION_USER_RE.sub("", text)
        text = _USER_URL_RE.sub("", text)
        text = _MENTION_PAGE_RE.sub(lambda m: (m.group(1) or "").strip(), text)
        text = _MENTION_DATE_RE.sub(lambda m: m.group(1), text)
        text = _FOOTNOTE_RE.sub("", text)
        if self._term_re is not None:
            text = self._term_re.sub(lambda m: self._repl.get(m.group(0).lower(), NAME_REPLACEMENT), text)
        return text


_MD_UNESCAPE_CHARS = "<>*_~|"
# Notion also escapes these inside code (observed: `\\~`, `\\|` in fenced code).
_MD_UNESCAPE_ALWAYS = "~|"


class MarkdownUnescaper:
    """Notion's markdown serializer backslash-escapes characters the model wrote
    literally (\\< \\> \\* \\_ \\~) in prose, but not inside code. Undo it outside
    fenced / inline code. Stateful so it can run over a stream of chunks."""

    def __init__(self):
        self.in_fence = False
        self.in_inline = False

    def process(self, text: str) -> str:
        if not text or ("\\" not in text and "`" not in text and "~~~" not in text):
            return text
        out = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            tri = text[i:i + 3]
            if tri in ("```", "~~~"):
                self.in_fence = not self.in_fence
                self.in_inline = False
                out.append(tri); i += 3; continue
            if ch == "\n":
                self.in_inline = False
            elif ch == "`" and not self.in_fence:
                self.in_inline = not self.in_inline
            elif ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt in _MD_UNESCAPE_ALWAYS or (
                        nxt in _MD_UNESCAPE_CHARS and not self.in_fence and not self.in_inline):
                    out.append(nxt)
                else:
                    out.append(ch + nxt)
                i += 2; continue
            out.append(ch); i += 1
        return "".join(out)


def unescape_markdown(text: str) -> str:
    return MarkdownUnescaper().process(text)



class StreamRedactor:
    """Incremental redaction for delta streams; holds back a short tail so terms
    split across chunks are still caught."""

    def __init__(self, redactor: IdentityRedactor):
        self.r = redactor
        self.buf = ""
        self.md = MarkdownUnescaper()

    def feed(self, text: str) -> str:
        self.buf += text or ""
        raw = self.buf
        cut = len(raw) - self.r._hold
        if cut <= 0:
            return ""
        # do not cut inside a word
        while cut > 0 and (raw[cut - 1].isalnum() or raw[cut - 1] in "@._-"):
            cut -= 1
        # nor inside an unfinished tag / footnote
        lt = raw.rfind("<", 0, cut)
        if lt != -1 and ">" not in raw[lt:cut]:
            cut = lt
        fn = raw.rfind("[^", 0, cut)
        if fn != -1 and "]" not in raw[fn:cut]:
            cut = fn
        # nor inside any match
        for pat in self.r.patterns():
            for m in pat.finditer(raw):
                if m.start() < cut < m.end():
                    cut = m.start()
        # nor right after a backslash or inside a run of backticks / tildes
        while cut > 0 and raw[cut - 1] in "\\`~":
            cut -= 1
        if cut <= 0:
            return ""
        self.buf = raw[cut:]
        return self.md.process(self.r.redact(raw[:cut]))

    def flush(self) -> str:
        out = self.md.process(self.r.redact(self.buf))
        self.buf = ""
        return out


def redact_stream(items, redactor: Optional[IdentityRedactor]):
    """Wrap a parse_stream item generator, scrubbing text-bearing items."""
    if redactor is None:
        yield from items
        return
    streams = {}
    last_meta = {}
    for item in items:
        if not isinstance(item, dict):
            yield item
            continue
        typ = item.get("type")
        if typ in ("content", "thinking") and isinstance(item.get("text"), str):
            sr = streams.setdefault(typ, StreamRedactor(redactor))
            last_meta[typ] = {k: v for k, v in item.items() if k != "text"}
            out = sr.feed(item["text"])
            if out:
                yield {**item, "text": out}
            continue
        if typ == "final_content":
            # flush pending deltas first so ordering is preserved
            for t, sr in streams.items():
                rest = sr.flush()
                if rest:
                    yield {**last_meta[t], "text": rest}
            new = dict(item)
            for k in ("text", "content"):
                if isinstance(new.get(k), str):
                    new[k] = unescape_markdown(redactor.redact(new[k]))
            yield new
            continue
        yield item
    for t, sr in streams.items():
        rest = sr.flush()
        if rest:
            yield {**last_meta[t], "text": rest}
