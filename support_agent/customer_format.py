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


# Owner 17.11.2026: the VPN names themselves carry the owner's referral links.
# Only these approved links become clickable names; model text can never add links.
REF_LINKS = {
    'DureVpnBot': 'https://telegram.me/DureVpnBot?start=YYFpHPog9',
    'v2raytnn_bot': 'https://t.me/v2raytnn_bot?start=facdaf71-69b8-4155-a3f7-bc245a6135fe',
}


def _referral_names(text):
    for name, url in REF_LINKS.items():
        # «@Name - link» / «@Name: link» / «@Name (link)» → just the name; the link goes under it.
        text = re.sub(r'@' + re.escape(name) + r'\s*(?:[-—–:]\s*|\()\s*' + re.escape(url) + r'\)?', '@' + name, text)
        text = re.sub(r'(?<![\w/@])' + re.escape(url) + r'(?![\w?=&-])', '@' + name, text)
    return text


def _link_names(rendered):
    for name, url in REF_LINKS.items():
        rendered = re.sub(r'(?<![\w/>])@' + re.escape(name) + r'(?!\w)',
                          '<a href="' + html.escape(url) + '">' + name + '</a>', rendered)
    return rendered


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
    text=_referral_names(text)
    # Owner 25.11.2026: a bullet glued into the middle of a line starts its own line, so options are separated.
    text=re.sub(r'(?<=[^\s\n])[ \t]+•[ \t]+','\n\n• ',text)
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
        bullet=re.match(r'^(•\s+)([^:\n—–]{2,70}?)(\s+[-—–]\s+|:\s+)(.*)$',formatted)
        if bullet and '<' not in bullet.group(2):
            # Owner 25.11.2026: the option or tariff name in a bullet is bold, so long answers are easy to scan.
            formatted=bullet.group(1)+'<b>'+bullet.group(2)+'</b>'+bullet.group(3)+bullet.group(4)
        # A short standalone heading or complete price line gets restrained
        # emphasis. Account emails, URLs and long prose are never guessed at.
        if ('<' not in formatted and len(line)<=100 and
                (line.endswith(':') or re.fullmatch(r'[^\n:]{1,65}:\s*\d[\d .,]*\s*(?:₽|руб(?:лей|ля|ль)?\.?|RUB)',line,re.I))):
            formatted='<b>'+formatted+'</b>'
        rendered.append(formatted)
    rendered=_link_names('\n'.join(rendered))
    plain=html.unescape(re.sub(r'</?(?:b|i|code|a)(?: href="[^"]*")?>','',rendered))
    return plain,rendered


def customer_payload(value):
    plain,rendered=format_customer(value)
    return plain,{'text':rendered,'parse_mode':'HTML',
                  'link_preview_options':{'is_disabled':True}}
