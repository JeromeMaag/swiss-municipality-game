"""Query helpers for game views and services."""

from django.db.models import Prefetch

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


def get_finished_game_summary(user, game_id: int) -> Game | None:
    """Return a finished game with all summary relationships for a user.

    Args:
        user: User who owns the game.
        game_id: Finished game primary key.

    Returns:
        The finished game with ordered turns, targets, cantons, and guesses, or
        None when the game does not exist or is not available for summaries.
    """
    turns = (
        Turn.objects.select_related("target__canton", "guess")
        .defer(
            "target__geom",
            "target__geom_simplified",
            "target__label_point",
            "target__canton__geom",
            "target__canton__geom_simplified",
            "target__canton__label_point",
        )
        .order_by("turn_number")
    )
    return (
        Game.objects.filter(user=user, status=Game.Status.FINISHED, pk=game_id)
        .prefetch_related(Prefetch("turns", queryset=turns))
        .first()
    )
