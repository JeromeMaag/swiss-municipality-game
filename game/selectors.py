"""Query helpers for game views and services."""

from .models import Game, Turn


def get_active_game(user) -> Game | None:
    """Return the newest active game for a user.

    Args:
        user: User whose active game should be returned.

    Returns:
        The active game or None.
    """
    return (
        Game.objects.filter(user=user, status=Game.Status.ACTIVE)
        .order_by("-started_at", "-id")
        .first()
    )


def get_current_turn(game: Game | None) -> Turn | None:
    """Return the first unrevealed turn for a game.

    Args:
        game: Game whose current turn should be returned.

    Returns:
        The current turn or None.
    """
    if game is None:
        return None
    return game.turns.filter(revealed_at__isnull=True).order_by("turn_number").first()
