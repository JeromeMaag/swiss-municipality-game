"""Query helpers for game views and services."""

from django.db.models import Prefetch, QuerySet

from .identity import PlayerIdentity
from .models import Game, Turn


def get_active_game(user) -> Game | None:
    """Return the newest active game for a user.

    Args:
        user: User whose active game should be returned.

    Returns:
        The active game or None.
    """
    return get_active_game_for_player(PlayerIdentity.for_user(user))


def get_active_game_for_player(player: PlayerIdentity) -> Game | None:
    """Return the newest active game for a player identity.

    Args:
        player: User or guest identity whose game should be returned.

    Returns:
        The active game or None.
    """
    return (
        Game.objects.filter(player.owner_query(), status=Game.Status.ACTIVE)
        .select_related("canton", "dataset_version")
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
    return get_finished_game_summary_for_player(PlayerIdentity.for_user(user), game_id)


def get_finished_games_for_player(player: PlayerIdentity) -> QuerySet[Game]:
    """Return finished games for a player ordered by newest first.

    Args:
        player: User or guest identity whose finished games should be returned.

    Returns:
        QuerySet of finished games owned by the player.
    """
    return (
        Game.objects.filter(player.owner_query(), status=Game.Status.FINISHED)
        .only(
            "id",
            "user",
            "guest_key",
            "target_type",
            "mode",
            "canton",
            "canton__abbreviation",
            "dataset_version",
            "status",
            "total_score",
            "started_at",
            "finished_at",
        )
        .select_related("canton", "dataset_version")
        .order_by("-finished_at", "-id")
    )


def get_finished_game_summary_for_player(
    player: PlayerIdentity,
    game_id: int,
) -> Game | None:
    """Return a finished summary game for a player identity.

    Args:
        player: User or guest identity that owns the game.
        game_id: Finished game primary key.

    Returns:
        The finished game with ordered turns, targets, cantons, and guesses, or
        None when the game does not exist or is not available for summaries.
    """
    turns = (
        Turn.objects.select_related(
            "game",
            "municipality_target__canton",
            "village_target__canton",
            "guess",
        )
        .defer(
            "municipality_target__geom",
            "municipality_target__geom_simplified",
            "municipality_target__label_point",
            "municipality_target__canton__geom",
            "municipality_target__canton__geom_simplified",
            "municipality_target__canton__label_point",
            "village_target__geom",
            "village_target__geom_simplified",
            "village_target__label_point",
            "village_target__canton__geom",
            "village_target__canton__geom_simplified",
            "village_target__canton__label_point",
        )
        .order_by("turn_number")
    )
    return (
        Game.objects.filter(
            player.owner_query(),
            status=Game.Status.FINISHED,
            pk=game_id,
        )
        .select_related("canton", "dataset_version")
        .prefetch_related(Prefetch("turns", queryset=turns))
        .first()
    )
