"""Pure policy primitives. No Telegram, model, database or production imports.

These checks are necessary conditions, not authorization to send a message.
The future worker must re-read persistent state immediately before dispatch.
"""
from dataclasses import dataclass, replace
from enum import Enum


class Mode(str, Enum):
    OFF = "off"
    DRAFT = "draft"
    AUTO = "auto"


@dataclass(frozen=True)
class Control:
    global_mode: Mode = Mode.OFF
    chat_mode: Mode = Mode.OFF
    global_revision: int = 0
    chat_revision: int = 0
    connection_enabled: bool = False
    can_reply: bool = False
    chat_allowed: bool = False


@dataclass(frozen=True)
class Candidate:
    global_revision: int
    chat_revision: int
    inbound_message_id: int
    created_at: float


def set_chat_mode(control: Control, mode: Mode) -> Control:
    """Even OFF -> AUTO invalidates answers created before the transition."""
    return replace(control, chat_mode=Mode(mode), chat_revision=control.chat_revision + 1)


def set_global_mode(control: Control, mode: Mode) -> Control:
    return replace(control, global_mode=Mode(mode), global_revision=control.global_revision + 1)


def send_block_reason(control, candidate, *, now, last_inbound_at,
                      latest_inbound_message_id, max_candidate_age=120):
    """Return None only if scheduling checks pass; caller still needs content checks.

Times are trusted server timestamps in seconds, never values supplied by a model.
Missing/unreadable persisted control must be supplied as None, not cached AUTO.
"""
    if not isinstance(control, Control):
        return "control_unavailable"
    if control.global_mode != Mode.AUTO or control.chat_mode != Mode.AUTO:
        return "manual_or_draft"
    if not (control.connection_enabled and control.can_reply and control.chat_allowed):
        return "connection_or_scope"
    if (candidate.global_revision, candidate.chat_revision) != (
            control.global_revision, control.chat_revision):
        return "control_changed"
    if candidate.inbound_message_id != latest_inbound_message_id:
        return "newer_message"
    if not 0 <= now - candidate.created_at <= max_candidate_age:
        return "stale_candidate"
    # A small conservative margin avoids dispatching at the 24-hour boundary.
    if last_inbound_at is None or not 0 <= now - last_inbound_at < 24 * 3600 - 60:
        return "reply_window"
    return None


def payment_evidence(row, *, authenticated_user_id, observed_at):
    """Project an owned DB row into minimal facts; no arbitrary text reaches AI.

Only the future trusted read adapter may call this. Identity must come from the
Telegram update and the query must ALSO restrict user_id. observed_at denotes
when the local DB was read, not when FreeKassa or the supplier was checked.
Payment confirmation deliberately makes no claim about subscription activation.
"""
    if not row or type(authenticated_user_id) is not int or authenticated_user_id <= 0:
        return None
    if type(row.get("user_id")) is not int or row["user_id"] != authenticated_user_id:
        return None
    state = row.get("status")
    if state not in {"pending", "paid", "failed", "cancelled", "refunded"}:
        state = "unknown"
    return {
        "source": "shop_database",
        "observed_at": observed_at,
        "payment_status": state,
        "activation_status": "not_checked",
    }
