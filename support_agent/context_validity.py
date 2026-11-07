"""One temporal contract for client history, independent of product names.

Callers supply database rows newest first, scoped to one chat and client role.
Never filter out invalid timestamps in SQL: such a row is a context boundary,
not permission to jump to older history. This module does no persistence or I/O.
"""
from dataclasses import dataclass, field
import math
from typing import Literal, TypedDict


Reason = Literal['accepted', 'empty', 'invalid_row', 'missing_timestamp',
                 'invalid_timestamp', 'future_timestamp', 'expired',
                 'session_gap', 'invalid_order', 'message_limit',
                 'character_limit', 'missing_source']


class ClientRow(TypedDict):
    mid: int
    text: str
    at: float


@dataclass(frozen=True)
class ContextValidity:
    # Source content is internal evidence; never interpolate it into diagnostics.
    rows: tuple[ClientRow, ...] = field(repr=False)
    reason: Reason
    checked_count: int
    character_count: int
    missing_source_count: int = 0

    @property
    def complete(self) -> bool:
        return self.reason == 'accepted'

    @property
    def diagnostic(self) -> str:
        return ('Этап: проверка истории клиента. Причина=' + self.reason
                + '; проверено=' + str(self.checked_count)
                + '; подтверждено=' + str(len(self.rows))
                + '; символов=' + str(self.character_count)
                + '; отсутствующих источников=' + str(self.missing_source_count) + '.')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_client_rows(rows, *, now, max_age=86400, session_gap=14400,
                         max_messages=60, max_chars=24000) -> ContextValidity:
    """Return only the contiguous validated recent rows, oldest first.

    A result may contain newer usable rows even when an older row establishes
    a boundary. A cached reference may use them only after require_sources().
    Freshness is checked against now for *every* row; the latest row must also
    belong to the current session. Missing timestamps are never validated.
    Role/chat are scoped by the database query; if role is present it must be
    client, so accidentally passing mixed-role rows fails closed.
    """
    if (not _number(now) or not _number(max_age) or max_age < 0
            or not _number(session_gap) or session_gap < 0
            or type(max_messages) is not int or max_messages < 1
            or type(max_chars) is not int or max_chars < 1):
        raise ValueError('Invalid client context validity limits')
    kept = []
    chars = checked = 0
    later = float(now)
    last_mid = None

    def result(reason):
        return ContextValidity(tuple(reversed(kept)), reason, checked, chars)

    for source in rows:
        checked += 1
        if len(kept) >= max_messages:
            return result('message_limit')
        try:
            row = dict(source)
        except (TypeError, ValueError):
            return result('invalid_row')
        mid = row.get('mid')
        text = row.get('text')
        if (type(mid) is not int or mid < 0 or row.get('role', 'client') != 'client'
                or text is not None and not isinstance(text, str)):
            return result('invalid_row')
        if last_mid is not None and mid >= last_mid:
            return result('invalid_order')
        at = row.get('at')
        if at is None:
            return result('missing_timestamp')
        if not _number(at):
            return result('invalid_timestamp')
        if at > now:
            return result('future_timestamp')
        if now - at > max_age:
            return result('expired')
        if at > later:
            return result('invalid_order')
        if later - at > session_gap:
            return result('session_gap')
        value = text or ''
        if chars + len(value) > max_chars:
            return result('character_limit')
        kept.append(ClientRow(mid=mid, text=value, at=float(at)))
        chars += len(value)
        later, last_mid = at, mid
    return result('accepted' if kept else 'empty')


def require_sources(context: ContextValidity, source_mids) -> ContextValidity:
    """Fail closed if a stored reference cannot revalidate all original sources.

    Removed, expired and out-of-bounds messages are all unavailable evidence.
    The upstream validity reason remains available on the original result.
    No replacement source is chosen from neighbouring messages.
    """
    mids = tuple(source_mids)
    if not mids or any(type(mid) is not int or mid < 0 for mid in mids):
        return ContextValidity((), 'missing_source', context.checked_count, 0,
                               max(1, len(mids)))
    present = {row['mid'] for row in context.rows}
    missing = len(set(mids) - present)
    if missing:
        return ContextValidity((), 'missing_source', context.checked_count, 0, missing)
    return context
