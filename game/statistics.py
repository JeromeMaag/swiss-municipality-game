"""Statistics helpers for player profile pages."""

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
    games = list(
        Game.objects.filter(user=user, status=Game.Status.FINISHED)
        .only("id", "total_score", "finished_at")
        .order_by("-finished_at", "-id")
    )
    distances = list(
        Guess.objects.filter(
            turn__game__user=user,
            turn__game__status=Game.Status.FINISHED,
        ).values_list("distance_to_municipality_m", flat=True)
    )
    games_played = len(games)
    rounds_played = len(distances)
    score_total = sum(game.total_score for game in games)
    average_score = round(score_total / games_played) if games_played else 0
    average_distance = (
        round(sum(distances) / rounds_played) if rounds_played else 0
    )
    return {
        "average_distance_m": average_distance,
        "average_score": average_score,
        "best_distance_m": round(min(distances)) if distances else 0,
        "best_score": max((game.total_score for game in games), default=0),
        "games_played": games_played,
        "map_modes": [
            {
                "average_score": average_score,
                "games_played": games_played,
                "label": DEFAULT_MAP_LABEL,
            }
        ],
        "perfect_rounds": sum(1 for distance in distances if round(distance) == 0),
        "recent_games": games[:RECENT_GAME_LIMIT],
        "rounds_played": rounds_played,
    }
