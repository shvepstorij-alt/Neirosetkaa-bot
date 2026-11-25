"""Owner 25.11.2026: no repeats. One pass over every outgoing reply:

1. a sentence that says the same as an earlier sentence of the same reply is dropped;
2. a sentence the assistant already said in its last replies of this chat is dropped
   (numbered steps and lines with links stay — the client may need them again).
If too little would remain, the reply is left as it was.
"""
import re

_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«"•])')
_WORD = re.compile(r'[а-яёa-z0-9]{4,}', re.I)


def _key(sentence):
    return {w.casefold()[:6] for w in _WORD.findall(sentence)}


def _same(a, b):
    if len(a) < 4 or len(b) < 4:
        return False
    return len(a & b) / len(a | b) >= 0.6 or (len(a & b) / min(len(a), len(b)) >= 0.85 and min(len(a), len(b)) >= 6)


def _protected(line):
    return bool(re.match(r'\s*\d+[.)]\s', line) or re.search(r'https?://|@\w{4,}_bot|chatgpt\.com/api', line))


def dedupe(reply, earlier=()):
    text = str(reply or '')
    seen = [_key(s) for message in earlier or () for s in _SPLIT.split(str(message or '')) if s.strip()]
    mine, out_paragraphs, dropped = [], [], 0
    for paragraph in re.split(r'\n\s*\n', text):
        kept_lines = []
        for line in paragraph.split('\n'):
            if _protected(line):
                kept_lines.append(line)
                continue
            parts = []
            for sentence in _SPLIT.split(line):
                key = _key(sentence)
                if any(_same(key, k) for k in mine) or any(_same(key, k) for k in seen):
                    dropped += 1
                    continue
                mine.append(key)
                parts.append(sentence)
            if parts:
                kept_lines.append(' '.join(parts))
        kept = '\n'.join(kept_lines).strip()
        if kept and not re.fullmatch(r'[^:\n]{1,80}:', kept):
            out_paragraphs.append(kept)
    # A whole paragraph that only retells what was said before (in this reply or the last ones) goes too.
    pool = set().union(*seen) if seen else set()
    final = []
    for paragraph in out_paragraphs:
        key = _key(paragraph)
        # The first paragraph is the direct answer and always stays.
        if (final and len(key) >= 8 and not any(_protected(line) for line in paragraph.split('\n'))
                and len(key & pool) / len(key) >= 0.6):
            dropped += 1
            continue
        final.append(paragraph)
        pool |= key
    result = '\n\n'.join(final).strip()
    if not dropped or len(result) < 40:
        return text
    return result


DATA_RECEIVED = ('Данные получил, спасибо! Александр активирует при первой возможности и напишет здесь. '
                 'Повторно ничего отправлять не нужно.')


def same_message(a, b):
    """True when two replies say practically the same (a resent instruction)."""
    ka, kb = _key(str(a or '')), _key(str(b or ''))
    return len(ka) >= 8 and len(kb) >= 8 and len(ka & kb) / len(ka | kb) >= 0.8


def resent_after_data(reply, rows):
    """rows: recent messages, newest first, dicts with role/text/media kind.
    The reply repeats an earlier instruction, and the client has already sent data after it → short status."""
    sent_data = False
    for row in rows:
        if row['role'] == 'client' and row.get('kind') in ('sensitive', 'manual_text_file'):
            sent_data = True
        if row['role'] == 'assistant' and same_message(reply, row['text']):
            return DATA_RECEIVED if sent_data else ''
        if row['role'] == 'owner':
            return ''
    return ''
