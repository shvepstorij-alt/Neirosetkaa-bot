"""Who may receive automatic replies. Defaults stay off; nothing here enables a client."""


def auto_recipients(raw_ids='', legacy_id='', for_all_raw='', allowed=(), owner=0):
    """Return (ids, for_all). IDs must already be allowed test clients, never the owner."""
    allowed = set(allowed)
    for_all = str(for_all_raw or '').strip().casefold() in ('1', 'yes', 'true', 'on')
    pieces = []
    for chunk in str(raw_ids or '').split(','):
        chunk = chunk.strip()
        if chunk:
            pieces.append(chunk)
    legacy = str(legacy_id or '').strip()
    if legacy:
        pieces.append(legacy)
    ids = set()
    for piece in pieces:
        if not piece.isdecimal():
            raise ValueError('Auto-reply client IDs must be numeric')
        value = int(piece)
        if value <= 0 or value == owner or value not in allowed:
            raise ValueError('Each auto-reply client must be a separate allowed test client')
        ids.add(value)
    if for_all and not allowed:
        raise ValueError('Auto-reply for everyone needs at least one allowed client')
    return frozenset(ids), for_all
