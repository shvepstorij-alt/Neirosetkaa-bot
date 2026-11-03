"""Reject versioned model names absent from supplied public evidence.

Presence is necessary, never sufficient: plan binding, negation, availability and
the source's accuracy still require semantic review. No web calls or memory.
"""
import re
import unicodedata


MODEL=re.compile(
    r'(?<!\w)(?:gpt[\s-]*\d+(?:\.\d+)?|'
    r'(?:claude\s+)?(?:opus|sonnet|haiku|mythos|fable)[\s-]+\d+(?:\.\d+)?|'
    r'gemini[\s-]+\d+(?:\.\d+)?)(?!\w|\.\d)',re.I)


def _normal(text):
    text=unicodedata.normalize('NFKC',str(text)).casefold()
    return text.replace('—','-').replace('–','-').replace('‑','-')


def unsupported_model_version(answer,facts):
    evidence=facts.get('public_research',{})
    if not evidence.get('sources'):return ''
    digest=_normal(evidence.get('digest',''))
    def key(text):return re.sub(r'[\s-]+','',re.sub(r'^claude\s+','',text))
    supported={key(match[0]) for match in MODEL.finditer(digest)}
    unknown=[]
    for match in MODEL.finditer(_normal(answer.get('reply',''))):
        if key(match[0]) not in supported and match[0] not in unknown:unknown.append(match[0])
    if not unknown:return ''
    return ('Этап: проверка версии модели. В предоставленной сводке источников не найдены '
            'названные в ответе версии: '+', '.join(unknown[:12])+'. Наличие других версий '
            'не подтверждает эти названия; не подставляй их из памяти.')
