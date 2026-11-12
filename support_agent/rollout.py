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


def _yes(value):
    return str(value or '').strip().casefold() in ('1', 'yes', 'true', 'on')


class AllowedChats:
    """Chats the assistant reads. Test IDs always; with drafts_for_all, every private client.

    Owner 12.11.2026: drafts for every client go to the owner. AUTO stays limited to the
    explicit test IDs (see Service.auto_permitted), so a new client never gets an
    automatic reply because of this switch.
    """

    def __init__(self, explicit=(), owner=0, drafts_for_all=False):
        self.explicit = frozenset(int(x) for x in explicit)
        self.owner = owner
        self.drafts_for_all = bool(drafts_for_all)

    def __contains__(self, uid):
        if uid in self.explicit:
            return True
        return (self.drafts_for_all and type(uid) is int and uid > 0 and uid != self.owner)

    def __iter__(self):
        return iter(sorted(self.explicit))

    def __len__(self):
        return len(self.explicit)

    def __bool__(self):
        return bool(self.explicit) or self.drafts_for_all


def allowed_chats(raw_ids='', owner=0, drafts_for_all_raw=''):
    ids = {int(x.strip()) for x in str(raw_ids or '').split(',') if x.strip()}
    return AllowedChats(ids, owner, _yes(drafts_for_all_raw))
