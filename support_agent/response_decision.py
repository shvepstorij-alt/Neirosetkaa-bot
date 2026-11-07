"""Process-local delivery proof for code-rendered, independently checked claims.

Only feature_claims' post-review renderer mints this contract. It distinguishes
coverage gaps from unsafe content; arbitrary partial model prose gets no proof.
An old process's saved answers require review again after restart.
"""
import hashlib
import hmac
import json
import math
import secrets

_KEY = secrets.token_bytes(32)
_FIELDS = {'version', 'kind', 'content_verified', 'coverage_complete',
           'owner_action_required', 'source_ids', 'selected_sources', 'checked_at', 'projection', 'proof'}


def _payload(text, decision):
    from .response_policy import natural_reply
    from .conversation_style import decorate_reply
    # Presentation may add/remove the code-owned salutation at persistence.
    # Canonicalize only that same transformation; substantive text stays bound.
    canonical = decorate_reply(natural_reply(text), {'continuing': True})
    return json.dumps({'reply': canonical.replace('\r\n', '\n').strip(),
                       'decision': {k: v for k, v in decision.items() if k != 'proof'}},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _seal_verified_atomic(answer, public_evidence):
    """Called only after accepted claims have been reviewed and rendered."""
    projection = answer.get('feature_projection', {})
    rendered = projection.get('rendered_claim_ids', [])
    gaps = projection.get('incomplete_target_ids', [])
    ids = answer.get('source_ids', [])
    # No accepted visible claims is an unresolved answer, never a verified
    # partial consultation merely because the gap itself is honest.
    selected = [{'id': s['id'], 'url': s.get('url')} for s in public_evidence.get('sources', [])
                if isinstance(s, dict) and s.get('id') in ids]
    checked_at = public_evidence.get('checked_at')
    if (not rendered or not ids or {s['id'] for s in selected} != set(ids)
            or type(checked_at) not in (int, float) or not math.isfinite(checked_at) or not checked_at > 0):
        return None
    decision = {'version': 1, 'kind': 'verified_atomic', 'content_verified': True,
                'coverage_complete': not bool(gaps), 'owner_action_required': False,
                'source_ids': list(ids), 'selected_sources': selected,
                'checked_at': checked_at, 'projection': projection}
    decision['proof'] = hmac.new(_KEY, _payload(answer['reply'], decision), hashlib.sha256).hexdigest()
    return decision


def verified_atomic_decision(answer, evidence):
    """Return a validated contract, or None; never trust model-written flags."""
    if not isinstance(answer, dict) or not isinstance(evidence, dict):
        return None
    decision = evidence.get('response_decision')
    if not isinstance(decision, dict) or set(decision) != _FIELDS:
        return None
    if (type(decision.get('version')) is not int or decision['version'] != 1
            or decision.get('kind') != 'verified_atomic'
            or decision.get('content_verified') is not True
            or type(decision.get('coverage_complete')) is not bool
            or decision.get('owner_action_required') is not False
            or evidence.get('owner_action_required') is True):
        return None
    projection = answer.get('feature_projection') or evidence.get('feature_projection')
    if not isinstance(projection, dict) or projection != decision.get('projection'):
        return None
    rendered = projection.get('rendered_claim_ids')
    gaps = projection.get('incomplete_target_ids')
    ids = decision.get('source_ids')
    if (not isinstance(rendered, list) or not rendered or not all(isinstance(i, str) for i in rendered)
            or not isinstance(gaps, list) or not all(isinstance(i, str) for i in gaps)
            or decision['coverage_complete'] != (not bool(gaps))
            or not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids)
            or len(ids) != len(set(ids))):
        return None
    if 'source_ids' in answer and answer['source_ids'] != ids:
        return None
    sources = evidence.get('sources', [])
    if not isinstance(sources, list):
        return None
    selected = [{'id': s.get('id'), 'url': s.get('url')} for s in sources
                if isinstance(s, dict) and s.get('id') in ids]
    if selected != decision.get('selected_sources') or evidence.get('checked_at') != decision.get('checked_at'):
        return None
    if not isinstance(answer.get('reply'), str):
        return None
    proof = decision.get('proof')
    if not isinstance(proof, str) or len(proof) != 64:
        return None
    try:
        expected = hmac.new(_KEY, _payload(answer['reply'], decision), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return None
    return decision if hmac.compare_digest(proof, expected) else None
