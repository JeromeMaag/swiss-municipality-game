"""Service helpers for persisted tracking events."""

from typing import Any

from .models import GameEvent


def track_event(
    user=None,
    event_type: str = "",
    session_key: str = "",
    game=None,
    turn=None,
    payload: dict[str, Any] | None = None,
) -> GameEvent:
    """Create and validate a game tracking event.

    Args:
        user: Optional authenticated user associated with the event.
        event_type: Event type value.
        session_key: Optional guest session key associated with the event.
        game: Optional linked game.
        turn: Optional linked turn.
        payload: Optional JSON payload.

    Returns:
        The persisted game event.

    Raises:
        ValidationError: If the event data or relationships are inconsistent.
    """
    event = GameEvent(
        user=user,
        session_key=session_key,
        game=game,
        turn=turn,
        event_type=event_type,
        payload=payload or {},
    )
    event.full_clean()
    event.save()
    return event
