"""Plan differences from official pages, checked on a known date. Prices never live here."""
import os

import yaml

_PATH = os.path.join(os.path.dirname(__file__), 'data', 'service_guide.yaml')


def load_guide():
    try:
        with open(_PATH, encoding='utf-8') as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def for_context():
    data = load_guide()
    return {'checked': str(data.get('checked') or ''),
            'choose': data.get('choose') or {},
            'services': data.get('services') or {}}
