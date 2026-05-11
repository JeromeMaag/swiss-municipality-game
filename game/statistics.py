"""Statistics helpers for player profile pages."""

from django.db.models import Avg, Count, Max, Min, Q

from .models import Game, Guess


DEFAULT_MAP_LABEL = "CH"
RECENT_GAME_LIMIT = 5


def build_player_statistics(user) -> dict:
    """Build profile statistics for an authenticated user.

    Args:
        user: User whose finished games should be summarized.

    Returns:
        A dictionary with aggregate score, distance, mode, and recent-game data.
    """
    finished_games = Game.objects.filter(user=user, status=Game.Status.FINISHED)
    game_stats = finished_games.aggregate(
        average_score=Avg("total_score"),
        best_score=Max("total_score"),
        games_played=Count("id"),
    )
    recent_games = list(
        finished_games.only(
            "id",
            "mode",
            "canton",
            "canton__abbreviation",
            "total_score",
            "finished_at",
        )
        .select_related("canton")
        .order_by("-finished_at", "-id")[:RECENT_GAME_LIMIT]
    )
    finished_guesses = Guess.objects.filter(
        user=user,
        turn__game__status=Game.Status.FINISHED,
    )
    distance_stats = finished_guesses.aggregate(
        average_distance_m=Avg("distance_to_municipality_m"),
        best_distance_m=Min("distance_to_municipality_m"),
        perfect_rounds=Count("id", filter=Q(distance_to_municipality_m=0)),
        rounds_played=Count("id"),
    )
    games_played = game_stats["games_played"] or 0
    average_score = round_or_zero(game_stats["average_score"])
    map_modes = list(
        finished_games.values("mode", "canton__abbreviation")
        .annotate(
            average_score=Avg("total_score"),
            games_played=Count("id"),
        )
        .order_by("mode", "canton__abbreviation")
    )
    return {
        "average_distance_m": round_or_zero(distance_stats["average_distance_m"]),
        "average_score": average_score,
        "best_distance_m": round_or_zero(distance_stats["best_distance_m"]),
        "best_score": game_stats["best_score"] or 0,
        "games_played": games_played,
        "map_modes": [
            {
                "average_score": round_or_zero(mode["average_score"]),
                "games_played": mode["games_played"],
                "label": map_mode_label(mode),
            }
            for mode in map_modes
        ],
        "perfect_rounds": distance_stats["perfect_rounds"] or 0,
        "recent_games": recent_games,
        "rounds_played": distance_stats["rounds_played"] or 0,
    }


def round_or_zero(value) -> int:
    """Round numeric aggregate values, using zero for empty result sets."""
    return round(value) if value is not None else 0


def map_mode_label(mode: dict) -> str:
    """Return a compact label for grouped map-mode statistics."""
    return mode["canton__abbreviation"] or DEFAULT_MAP_LABEL
