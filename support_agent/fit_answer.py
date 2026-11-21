"""Fit a reply to what the client asked: drop the variants for devices, consoles and services nobody asked about.

Owner 21.11.2026 («реши проблему системно»): an owner answer written for every case («iPhone: … / Android: •ChatGPT
•Claude») or a model reply covering several services was sent whole, so a client asking about Android got the
iPhone guide and Claude too. This runs last on every reply (playbook.tidy), FAQ or model text alike.

Rules:
* A paragraph that starts with a label («iPhone: …», «Android:», «• PlayStation: …», «Если это Claude: …») opens a
  section of that entity; the section runs until the next labelled paragraph of the same group.
* Inside a paragraph, a line starting with a label is a one-line item of that entity.
* A group (device, console, service) is filtered only when the client named some of its entities and the reply has
  variants for at least two of them. Text before the first label is always kept. If nothing would remain, the reply
  is returned unchanged.
"""
import functools
import re
from pathlib import Path

import yaml

# A clause about a device already done is not the question: «на ноут поставила» (owner case @tatnua_z).
_DONE = re.compile(r'(?i)(?:поставил|установил|скачал|загрузил)\w*|уже\s+(?:стоит|есть|работает)|работает\s+на')

_DEVICES = {
    'iPhone': (r'айфон\w*|iphone|\bios\b|ipad|айпад\w*|app\s*store|эп+\s*стор\w*|апп?\s*стор\w*|яблок\w*',
               r'iphone|ipad|ios\b|app\s*store'),
    'Android': (r'андро[ийеэ]д\w*|android|самсунг\w*|samsung|xiaomi|сяоми|редми|redmi|huawei|хуавей|honor|хонор|pixel|\bapk\b',
                r'android|google\s*play'),
    'Компьютер': (r'компьютер\w*|\bкомп\b|ноут\w*|ноутбук\w*|\bпк\b|\bpc\b|винд\w*|windows|\bмак\w*|\bmac\w*|макбук\w*|десктоп\w*|desktop',
                  r'компьютер\w*|windows|mac\b|macos'),
}
_CONSOLES = {
    'PlayStation': (r'playstation|\bps\s*\d?\b|\bпс\b|плейстейшн\w*|плойк\w*|сони', r'playstation|ps\s*\d|ps\s+plus'),
    'Xbox': (r'xbox|иксбокс\w*|хбокс\w*|game\s*pass', r'xbox'),
}
_EXTRA_SERVICE_WORDS = {'chatgpt': r'джи\s*пи\s*ти|джипити|\bчат\b|чатик', 'claude': r'клауд\w*|клоуд\w*'}


@functools.lru_cache(maxsize=1)
def _services():
    """{name: (client_pattern, label_pattern)} from aliases.yaml — the one list of service names."""
    path = Path(__file__).with_name('data') / 'aliases.yaml'
    try:
        rows = (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('services') or []
    except Exception:
        rows = []
    result = {}
    for row in rows:
        name = str(row.get('name') or '').strip()
        if not name:
            continue
        words = [str(w) for w in (row.get('aliases') or []) if str(w).strip()] + [name, str(row.get('id') or '')]
        client = '|'.join(r'(?<![\w])' + re.escape(w.strip()) + r'(?![\w])' for w in words if w.strip())
        extra = _EXTRA_SERVICE_WORDS.get(str(row.get('id') or ''))
        if extra:
            client += '|' + extra
        label = '|'.join(re.escape(w.strip()) for w in {name, str(row.get('id') or '')} if w.strip())
        result[name] = (client, label)
    return result


def _groups():
    return {'device': _DEVICES, 'console': _CONSOLES, 'service': _services()}


def _label(line, group):
    """The entity a line is labelled with, e.g. «• Claude: …», «iPhone: …», «Если это ChatGPT — …»."""
    head = re.sub(r'^\s*(?:[•\-–*]\s*|\d+[.)]\s*)?(?:если\s+(?:это|у\s+вас)\s+|для\s+)?', '', line, flags=re.I)
    for name, (_client, label) in group.items():
        if label and re.match(r'(?i)(?:' + label + r')\b[^\n:—]{0,40}?(?::|—)', head):
            return name
    return None


def _asked(burst, group, extra=''):
    wanted = ' '.join(part for part in re.split(r'[.!?\n]+', str(burst or '')) if not _DONE.search(part))
    text = wanted + ' ' + str(extra or '')
    return {name for name, (client, _label) in group.items() if client and re.search('(?i)(?:' + client + ')', text)}


def _section_label(para, group):
    """A paragraph opens a section only when its first line is labelled and no other line in it is:
    «Если это ChatGPT: …\nЕсли это Claude: …» is a list of items (filtered line by line), not a section."""
    lines = para.split('\n')
    if sum(1 for line in lines if _label(line, group)) > 1:
        return 'LIST'
    return _label(lines[0], group)


def _filter_sections(paragraphs, group, asked):
    labels = [_section_label(p, group) for p in paragraphs]
    present = {name for name in labels if name and name != 'LIST'}
    if len(present) < 2 or not (asked & present):
        return paragraphs
    kept, current = [], None
    for para, name in zip(paragraphs, labels):
        if name == 'LIST':
            current = None
        elif name:
            current = name
        if current is None or current in asked:
            kept.append(para)
    return kept


def _filter_lines(paragraphs, group, asked):
    result = []
    for para in paragraphs:
        lines = para.split('\n')
        labels = [_label(line, group) for line in lines]
        present = {name for name in labels if name}
        removed = False
        if len(present) >= 2 and asked & present:
            kept = [line for line, name in zip(lines, labels) if name is None or name in asked]
            removed = len(kept) != len(lines)
            lines = kept
        text = '\n'.join(lines).strip()
        if text and not (removed and re.fullmatch(r'[^\n]{1,40}:', text)):  # a heading whose items all went goes too
            result.append(text)
    return result


def fit_to_question(reply, burst, service=''):
    text = str(reply or '')
    if not text.strip() or not str(burst or '').strip():
        return text
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    for kind, group in _groups().items():
        if not group:
            continue
        asked = _asked(burst, group, service if kind == 'service' else '')
        if not asked:
            continue
        paragraphs = _filter_sections(paragraphs, group, asked)
        paragraphs = _filter_lines(paragraphs, group, asked)
    out = '\n\n'.join(paragraphs).strip()
    return out or text
