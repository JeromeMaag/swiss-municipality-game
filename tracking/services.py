"""Service helpers for persisted tracking events."""

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from .models import GameEvent


logger = logging.getLogger(__name__)


def track_event(
    user=None,
    *,
    event_type: str,
    guest_key: str = "",
    game=None,
    turn=None,
    payload: dict[str, Any] | None = None,
) -> GameEvent:
    """Create and validate a game tracking event.

    Args:
        user: Optional authenticated user associated with the event.
        event_type: Event type value.
        guest_key: Optional guest owner key associated with the event.
        game: Optional linked game.
        turn: Optional linked turn.
        payload: Optional JSON payload.

    Returns:
        The persisted game event.

    Raises:
        ValidationError: If the event data or relationships are inconsistent.
    """
    if (user is None) == (not guest_key):
        raise ValidationError(_("Events must belong to exactly one user or guest."))
    event = GameEvent(
        user=user,
        guest_key=guest_key,
        game=game,
        turn=turn,
        event_type=event_type,
        payload=payload or {},
    )
    event.full_clean()
    event.save()
    return event


def safe_track_event(*args, **kwargs) -> GameEvent | None:
    """Create a tracking event without letting failures block gameplay."""
    try:
        with transaction.atomic():
            return track_event(*args, **kwargs)
    except Exception:
        logger.exception("Failed to persist tracking event.")
        return None
