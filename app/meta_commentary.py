"""Remove paragraphs where the Notion-hosted model comments on the gateway's own
scaffolding (instructions page, tool-format note, reminders) instead of the user's
request, e.g. "Your message also included text formatted as a Notion page with
instructions... I ignored those instructions." Only whole paragraphs at the start or
end of a reply are removed, never the only paragraph, never inside code fences."""
import re

_STRONG = re.compile(
    r"(?:your|the) message(?: you sent)? (?:also )?(?:included|contained|had)[^.\n]{0,100}(?:instruction|notion|guideline)|"
    r"notion page[^.\n]{0,80}instruction|instructions?[^.\n]{0,80}notion page|"
    r"ignor\w*[^.\n]{0,80}instructions?[^.\n]{0,100}(?:pasted|appended|after the|weren'?t part|aren'?t part|not part|embedded)|"
    r"instructions?[^.\n]{0,120}(?:aren'?t|weren'?t|isn'?t|not) part of your (?:question|request|message)|"
    r"(?:formatting|action[- ]block) (?:setup|note|instructions?)[^.\n]{0,80}(?:ignor|mention|noticed)|"
    r"消息(?:中|里)(?:还)?(?:包含|附带)[^。\n]{0,60}(?:指令|说明|Notion)|忽略了?[^。\n]{0,40}(?:指令|说明)[^。\n]{0,40}(?:不属于|并非)", re.I)
_SENT = re.compile(
    r"(?:pasted|appended|embedded|attached|included) (?:text|note|instructions?|block)[^.\n]{0,40}(?:says|said|asks|tells|wants|instructs)[^.\n]{0,40}\b(?:I|me)\b|"
    r"I(?:'m| am) not (?:following|going to follow|obeying) (?:those|these|the|that|any)[^.\n]{0,30}instructions?|"
    r"(?:your|the) message (?:says|asks|tells|instructs)[^.\n]{0,40}\b(?:I|me)\b[^.\n]{0,40}(?:describe|present|call|identify|act)", re.I)
_META = re.compile(_STRONG.pattern + "|" + _SENT.pattern, re.I)


def _strip_meta_sentences(text: str) -> str:
    out = []
    for par in re.split(r"(\n\s*\n)", text):
        if "```" in par or not _SENT.search(par):
            out.append(par); continue
        sents = re.split(r"(?<=[.!?])\s+", par)
        keep = [x for x in sents if not _SENT.search(x)]
        # drop the meta sentence and any directly following sentences that continue it
        out.append(" ".join(keep) if keep else par)
    return "".join(out)


def _is_meta(par: str) -> bool:
    p = par.strip()
    return bool(p) and len(p) < 600 and "```" not in p and bool(_STRONG.search(p))


def strip_meta_commentary(text: str) -> str:
    if not text or not _META.search(text):
        return text
    if _SENT.search(text):
        text = _strip_meta_sentences(text)
        if not _STRONG.search(text):
            return text
    pars = re.split(r"(\n\s*\n)", text)
    idx = [i for i in range(0, len(pars), 2)]
    content = [i for i in idx if pars[i].strip()]
    if len(content) < 2:
        return text
    drop = set()
    for i in content:            # leading meta paragraphs
        if _is_meta(pars[i]) and len(content) - len(drop) > 1:
            drop.add(i)
        else:
            break
    for i in reversed(content):  # trailing meta paragraphs
        if i in drop:
            break
        if _is_meta(pars[i]) and len(content) - len(drop) > 1:
            drop.add(i)
        else:
            break
    if not drop:
        return text
    keep = [pars[i] for i in content if i not in drop]
    return "\n\n".join(p.strip("\n") for p in keep)
