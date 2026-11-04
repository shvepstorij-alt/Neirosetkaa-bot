"""Controlled customer presentation; no model-authored HTML or link previews."""
import html
import re


def _inline(value):
    # Only emphasis is accepted. Escaping happens before adding our own tags, so
    # model-authored HTML can never create Telegram links, mentions or entities.
    safe=html.escape(value,quote=False)
    safe=re.sub(r'\*\*([^*\n]+)\*\*',r'<b>\1</b>',safe)
    safe=re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)',r'<i>\1</i>',safe)
    safe=re.sub(r'`([^`\n]+)`',r'<code>\1</code>',safe)
    return safe


def format_customer(value):
    """Return readable plain history and safe HTML with identical visible text.

    Never summarize or shorten facts, prices, account fields or instructions.
    Only existing long prose is broken at sentence boundaries. Numbered steps,
    prices, URLs and short paragraphs keep their boundaries.
    """
    text=str(value or '').replace('\r\n','\n').strip()
    # Telegram cannot render Markdown links in HTML mode. Preserve the visible
    # label AND URL rather than silently dropping a useful instruction target.
    text=re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)',r'\1 (\2)',text)
    blocks=[]
    for block in re.split(r'\n\s*\n',text):
        lines=[]
        for line in block.splitlines():
            line=line.strip()
            if len(line)>300 and not re.match(r'^\d+[.)]\s',line):
                # Match punctuation followed by a capital/number, not a decimal
                # or an internal URL dot. Never break URL/session paths.
                pieces=re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z0-9])',line)
                lines.extend(pieces)
            else:lines.append(line)
        blocks.append('\n'.join(lines))
    text='\n\n'.join(blocks)
    rendered=[]
    for line in text.split('\n'):
        formatted=_inline(line)
        # A short standalone heading or complete price line gets restrained
        # emphasis. Account emails, URLs and long prose are never guessed at.
        if ('<' not in formatted and len(line)<=100 and
                (line.endswith(':') or re.fullmatch(r'[^\n:]{1,65}:\s*\d[\d .,]*\s*(?:₽|руб(?:лей|ля|ль)?\.?|RUB)',line,re.I))):
            formatted='<b>'+formatted+'</b>'
        rendered.append(formatted)
    rendered='\n'.join(rendered)
    plain=html.unescape(re.sub(r'</?(?:b|i|code)>','',rendered))
    return plain,rendered


def customer_payload(value):
    plain,rendered=format_customer(value)
    return plain,{'text':rendered,'parse_mode':'HTML',
                  'link_preview_options':{'is_disabled':True}}
