"""Final-layer scrubber for Notion's provisional language markup (<lang primary="en-US">...</lang>).

Applied to every text item leaving NotionClient.stream_response (stream + non-stream, chat +
responses, tool + plain, recovered thread text). Stateful across chunks: a trailing fragment that
could still become a Notion lang tag is held back until it resolves. Only Notion's shapes are
removed: `<lang>`, `<lang primary=...>`, `</lang>`, and an unterminated `<lang ...` at the very
start / end of the answer. `<language>`, `<lang foo="x">`, and anything inside ``` fences
(except primary= tags) pass through untouched.
"""
import re

_TAG = re.compile(r'<lang\s+primary\s*=\s*"?[A-Za-z_\-]{0,20}"?\s*/?>|</lang\s*>')
_PRIMARY_TAG = re.compile(r'<lang\s+primary\s*=\s*"?[A-Za-z_\-]{0,20}"?\s*/?>')
# unterminated opener (no '>' yet): '<', '<l', ... '<lang', '<lang primary="en-', '</la'
def _partial_start(text: str) -> int:
    """Index of a trailing fragment that may still become a Notion lang tag, else -1."""
    i = text.rfind('<')
    if i == -1 or len(text) - i > 60:
        return -1
    frag = text[i:]
    if '>' in frag or '\n' in frag:
        return -1
    if '<lang'.startswith(frag) or '</lang'.startswith(frag):
        return i
    if re.fullmatch(r'<lang(?:\s[^<>]*)?|</lang\s*', frag):
        return i
    return -1
_START_JUNK = re.compile(r'^\s*<lang(?:\s*/?>|\s+primary\s*=\s*"?[A-Za-z_\-]{0,20}"?\s*/?>?'
                         r'|\s+(?:primary|primar|prima|prim|pri|pr|p)(?![a-z=])|(?=\s|$))[ \t]*')
_BARE = re.compile(r'^\s*<lang(?=\s|$)[ \t]*')


def _scrub(text: str, in_fence: bool) -> tuple[str, bool]:
    out = []
    parts = re.split(r'(```|`[^`\n]+`)', text)
    for p in parts:
        if p == '```':
            in_fence = not in_fence
            out.append(p)
            continue
        if in_fence or (len(p) > 1 and p.startswith('`') and p.endswith('`')):
            out.append(_PRIMARY_TAG.sub('', p))  # code: only Notion's own primary= tags
        else:
            out.append(_TAG.sub('', p))
    return ''.join(out), in_fence


class LangSanitizer:
    def __init__(self) -> None:
        self.buf = ''
        self.started = False  # emitted any non-space text yet
        self.in_fence = False

    def feed(self, chunk: str) -> str:
        text = self.buf + (chunk or '')
        self.buf = ''
        cut = _partial_start(text)
        if cut != -1:
            text, self.buf = text[:cut], text[cut:]
        return self._emit(text)

    def _emit(self, text: str, final: bool = False) -> str:
        if not text:
            return ''
        if not self.started:
            while True:
                m = _START_JUNK.match(text)
                if not m or m.end() == 0:
                    break
                bare = _BARE.match(text)
                if bare and bare.end() == m.end():
                    line = text[m.end():].split('\n', 1)[0]
                    if re.match(r'[^<>]*>', line):
                        break  # e.g. '<lang foo="x">' — not Notion's shape, keep
                text = text[m.end():]
        cleaned, self.in_fence = _scrub(text, self.in_fence)
        if not self.started and cleaned.strip():
            self.started = True
        return cleaned

    def flush(self) -> str:
        rest, self.buf = self.buf, ''
        if not rest:
            return ''
        if rest.startswith('</lang') or (not self.started and rest.strip() in ('<', '<l', '<la', '<lan')):
            return ''
        if rest.startswith('<lang'):
            if not self.started:
                return self._emit(rest, True)
            m = re.match(r'<lang(?:\s+(?:primary\s*=\s*"?[A-Za-z_\-]{0,20}"?|primar|prima|prim|pri|pr|p)(?![a-z=]))?\s*', rest)
            return self._emit(rest[m.end():], True)
        return self._emit(rest, True)

def clean_lang(text: str) -> str:
    s = LangSanitizer()
    return s.feed(text) + s.flush()
