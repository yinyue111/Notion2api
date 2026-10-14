"""Last-resort rewrite when every persona-guard attempt still answered as Notion AI:
drop sentences / list items that present the assistant as Notion AI or list Notion
workspace features, and lead with a neutral identity sentence."""
import re

_SENT_SPLIT = re.compile(r"(?<=[.!?])[ \t]+|(?<=[。！？])")
_NOTION = re.compile(r"notion|workspace|工作区|工作空间|pages? and databases|databases?|meeting notes|数据库|会议记录", re.I)
_CJK = re.compile(r"[\u4e00-\u9fff]")
EN = "I'm an AI assistant built on Claude by Anthropic."
ZH = "我是一个基于 Anthropic Claude 模型的 AI 助手。"


def rewrite_persona(text: str) -> str:
    if not text or "```" in text:
        return text
    out_lines, any_kept, dropped_first = [], False, False
    for line in text.split("\n"):
        if not line.strip():
            out_lines.append("")
            continue
        sents = [x for x in _SENT_SPLIT.split(line) if x]
        keep = []
        for x in sents:
            if _NOTION.search(x):
                if not any_kept and not keep:
                    dropped_first = True
                continue
            keep.append(x)
        if keep:
            sep = "" if _CJK.search(line) else " "
            out_lines.append(sep.join(k.strip() for k in keep) if not line.lstrip().startswith(("-", "*")) or len(keep) == len(sents) else "")
            any_kept = any_kept or bool(out_lines[-1].strip())
    # dangling list intros whose items were all removed
    res = []
    for i, l in enumerate(out_lines):
        nxt = next((x for x in out_lines[i + 1:] if x.strip()), "")
        if l.rstrip().endswith((":", "：")) and not nxt.lstrip().startswith(("-", "*", "1")):
            continue
        res.append(l)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(res)).strip()
    lead = ZH if _CJK.search(text) else EN
    if dropped_first or not body:
        return (lead + ("" if not body else ("\n\n" if body.lstrip().startswith(("-", "*")) else ("" if _CJK.search(lead) else " ")) + body)).strip()
    return body
