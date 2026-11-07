"""Owner-edited colloquial names. Passed to the model, never used to pick a service."""
from pathlib import Path

import yaml


_PATH = Path(__file__).with_name('data') / 'aliases.yaml'
_CACHE = None


def load_glossary():
    """Read the alias file. The same words stay on one service."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    raw = yaml.safe_load(_PATH.read_text(encoding='utf-8')) or {}
    services = []
    seen = {}
    for row in raw.get('services') or []:
        if not isinstance(row, dict):
            continue
        service_id = str(row.get('id') or '').strip()
        name = str(row.get('name') or service_id).strip()
        aliases = []
        for alias in row.get('aliases') or []:
            text = ' '.join(str(alias or '').split())
            if not text:
                continue
            key = text.casefold().replace('ё', 'е')
            owner = seen.get(key)
            if owner and owner != service_id:
                raise ValueError('Alias is listed for two services')
            seen[key] = service_id
            aliases.append(text)
        if service_id and name:
            services.append({'id': service_id, 'name': name, 'aliases': aliases})
    phrases = []
    for row in raw.get('phrases') or []:
        if not isinstance(row, dict):
            continue
        phrase = ' '.join(str(row.get('phrase') or '').split())
        service = str(row.get('service') or '').strip()
        if phrase and service:
            phrases.append({'phrase': phrase, 'service': service, 'tariff': str(row.get('tariff') or '').strip()})
    words = []
    for row in raw.get('tariff_words') or []:
        if not isinstance(row, dict):
            continue
        forms = [' '.join(str(item or '').split()) for item in row.get('words') or [] if str(item or '').strip()]
        meaning = ' '.join(str(row.get('meaning') or '').split())
        if forms and meaning:
            words.append({'words': forms, 'meaning': meaning})
    _CACHE = {'services': services, 'phrases': phrases, 'tariff_words': words}
    return _CACHE


def for_context(facts):
    """Glossary rows for services that are actually in this catalogue snapshot."""
    loaded = load_glossary()
    catalog = [row for row in (facts or {}).get('catalog') or [] if isinstance(row, dict)]
    known = {str(row.get('service_key') or '') for row in catalog}
    services = [dict(row) for row in loaded['services'] if row['id'] in known]
    present = {row['id'] for row in services}
    for row in catalog:
        service_id = str(row.get('service_key') or '')
        if not service_id or service_id in present:
            continue
        name = str(row.get('service') or service_id)
        services.append({'id': service_id, 'name': name, 'aliases': [name]})
        present.add(service_id)
    phrases = [row for row in loaded['phrases'] if row['service'] in present]
    return {'services': services, 'phrases': phrases, 'tariff_words': loaded['tariff_words']}
