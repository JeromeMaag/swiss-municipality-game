"""Statistics helpers for player profile pages."""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Avg, Count, Max, Min, Q
from django.db.models.functions import TruncWeek
from django.utils import timezone
from django.utils.translation import gettext as _

from geo.selectors import (
    get_current_cantons,
    get_current_dataset_version,
    get_municipalities_for_canton,
    get_municipalities_for_dataset,
    get_villages_for_canton,
    get_villages_for_dataset,
)

from .models import Game, Guess


DEFAULT_MAP_LABEL = "CH"
RECENT_GAME_LIMIT = 5
EXACT_HIT_SCORE = 1000
NEAR_HIT_MIN_SCORE = 800
TARGET_TABLE_LIMIT = 100

PERIOD_CHOICES = {
    "all": None,
    "30d": 30,
    "90d": 90,
    "365d": 365,
}
SORT_CHOICES = {
    "needs_practice",
    "hit_rate_desc",
    "score_desc",
    "attempts_desc",
    "distance_asc",
}
GAME_MODE_CHOICES = {"all", *Game.Mode.values}
TARGET_TYPE_CHOICES = {"all", *Game.TargetType.values}


@dataclass(frozen=True)
class AdvancedStatisticsFilters:
    """Normalized filter values for detailed personal statistics."""

    canton: str = "all"
    game_mode: str = "all"
    period: str = "all"
    sort: str = "needs_practice"
    target_type: str = "all"


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
            "target_type",
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
        finished_games.values("target_type", "mode", "canton__abbreviation")
        .annotate(
            average_score=Avg("total_score"),
            games_played=Count("id"),
        )
        .order_by("target_type", "mode", "canton__abbreviation")
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
                "target_label": target_type_label(mode["target_type"]),
                "target_type": mode["target_type"],
            }
            for mode in map_modes
        ],
        "perfect_rounds": distance_stats["perfect_rounds"] or 0,
        "recent_games": recent_games,
        "rounds_played": distance_stats["rounds_played"] or 0,
    }


def parse_advanced_statistics_filters(params) -> AdvancedStatisticsFilters:
    """Normalize query parameters for the detailed statistics page."""
    canton = (params.get("canton", "all") or "all").strip()
    if not canton:
        canton = "all"
    elif canton != "all":
        canton = canton.upper()
        canton_exists = get_current_cantons().filter(abbreviation=canton).exists()
        canton = canton if canton_exists else "all"
    game_mode = params.get("game_mode", "all") or "all"
    if game_mode not in GAME_MODE_CHOICES:
        game_mode = "all"
    period = params.get("period", "all") or "all"
    if period not in PERIOD_CHOICES:
        period = "all"
    sort = params.get("sort", "needs_practice") or "needs_practice"
    if sort not in SORT_CHOICES:
        sort = "needs_practice"
    target_type = params.get("target_type", "all") or "all"
    if target_type not in TARGET_TYPE_CHOICES:
        target_type = "all"
    return AdvancedStatisticsFilters(
        canton=canton,
        game_mode=game_mode,
        period=period,
        sort=sort,
        target_type=target_type,
    )


def build_player_advanced_statistics(user, filters: AdvancedStatisticsFilters) -> dict:
    """Build detailed per-canton and per-target statistics for one user."""
    guesses = filtered_finished_guesses(user, filters)
    target_stats = build_target_statistics(guesses, filters.sort, filters.target_type)
    visible_target_stats = target_stats
    target_stats_limited = False
    if filters.canton == "all" and len(target_stats) > TARGET_TABLE_LIMIT:
        visible_target_stats = target_stats[:TARGET_TABLE_LIMIT]
        target_stats_limited = True
    return {
        "available_cantons": list(
            get_current_cantons()
            .order_by("abbreviation")
            .values("abbreviation", "name")
            .distinct()
        ),
        "canton_stats": build_canton_statistics(guesses, filters.target_type),
        "filters": filters,
        "filter_options": build_filter_options(),
        "overview": build_advanced_overview(guesses, filters),
        "strengths": strongest_targets(target_stats),
        "target_stats": visible_target_stats,
        "target_stats_limited": target_stats_limited,
        "target_stats_total": len(target_stats),
        "trend": build_weekly_trend(guesses),
        "weaknesses": weakest_targets(target_stats),
    }


def build_filter_options() -> dict:
    """Return translated filter option labels for the detail statistics UI."""
    return {
        "game_modes": [
            {"value": "all", "label": _("All modes")},
            {"value": Game.Mode.SWITZERLAND, "label": _("Switzerland")},
            {"value": Game.Mode.CANTON, "label": _("Single canton")},
        ],
        "periods": [
            {"value": "all", "label": _("All time")},
            {"value": "30d", "label": _("Last 30 days")},
            {"value": "90d", "label": _("Last 90 days")},
            {"value": "365d", "label": _("Last year")},
        ],
        "sorts": [
            {"value": "needs_practice", "label": _("Needs practice")},
            {"value": "hit_rate_desc", "label": _("Best hit rate")},
            {"value": "score_desc", "label": _("Best average score")},
            {"value": "attempts_desc", "label": _("Most attempts")},
            {"value": "distance_asc", "label": _("Best average distance")},
        ],
        "target_types": [
            {"value": "all", "label": _("All targets")},
            {"value": Game.TargetType.MUNICIPALITY, "label": _("Municipalities")},
            {"value": Game.TargetType.VILLAGE, "label": _("Villages")},
        ],
    }


def filtered_finished_guesses(user, filters: AdvancedStatisticsFilters):
    """Return finished-game guesses matching detailed statistics filters."""
    guesses = Guess.objects.filter(
        user=user,
        turn__game__status=Game.Status.FINISHED,
    ).select_related(
        "turn__game",
        "turn__game__canton",
        "turn__municipality_target",
        "turn__municipality_target__canton",
        "turn__village_target",
        "turn__village_target__canton",
    )
    if filters.target_type != "all":
        guesses = guesses.filter(turn__game__target_type=filters.target_type)
    if filters.game_mode != "all":
        guesses = guesses.filter(turn__game__mode=filters.game_mode)
    if filters.canton != "all":
        guesses = guesses.filter(
            Q(turn__municipality_target__canton__abbreviation=filters.canton)
            | Q(turn__village_target__canton__abbreviation=filters.canton)
        )
    period_days = PERIOD_CHOICES[filters.period]
    if period_days is not None:
        guesses = guesses.filter(
            guessed_at__gte=timezone.now() - timedelta(days=period_days)
        )
    return guesses


def build_advanced_overview(guesses, filters: AdvancedStatisticsFilters) -> dict:
    """Return high-level round statistics for the selected filter scope."""
    aggregate = guesses.aggregate(
        average_distance_m=Avg("distance_to_municipality_m"),
        average_score=Avg("score"),
        best_distance_m=Min("distance_to_municipality_m"),
        best_score=Max("score"),
        games_played=Count("turn__game", distinct=True),
        hits=Count("id", filter=Q(score=EXACT_HIT_SCORE)),
        near_hits=Count(
            "id",
            filter=Q(score__gte=NEAR_HIT_MIN_SCORE, score__lt=EXACT_HIT_SCORE),
        ),
        rounds_played=Count("id"),
    )
    rounds_played = aggregate["rounds_played"] or 0
    targets_played = count_distinct_played_targets(guesses, filters)
    total_targets = count_total_targets(filters)
    return {
        "average_distance_m": round_or_zero(aggregate["average_distance_m"]),
        "average_score": round_or_zero(aggregate["average_score"]),
        "best_distance_m": round_or_zero(aggregate["best_distance_m"]),
        "best_score": aggregate["best_score"] or 0,
        "coverage_percent": percentage(targets_played, total_targets),
        "games_played": aggregate["games_played"] or 0,
        "hit_rate": percentage(aggregate["hits"] or 0, rounds_played),
        "hits": aggregate["hits"] or 0,
        "near_hit_rate": percentage(aggregate["near_hits"] or 0, rounds_played),
        "near_hits": aggregate["near_hits"] or 0,
        "rounds_played": rounds_played,
        "targets_played": targets_played,
        "total_targets": total_targets,
    }


def count_distinct_played_targets(guesses, filters: AdvancedStatisticsFilters) -> int:
    """Return distinct target count for the filtered guesses."""
    total = 0
    if filters.target_type in ("all", Game.TargetType.MUNICIPALITY):
        total += (
            guesses.filter(turn__game__target_type=Game.TargetType.MUNICIPALITY)
            .values("turn__municipality_target_id")
            .distinct()
            .count()
        )
    if filters.target_type in ("all", Game.TargetType.VILLAGE):
        total += (
            guesses.filter(turn__game__target_type=Game.TargetType.VILLAGE)
            .values("turn__village_target_id")
            .distinct()
            .count()
        )
    return total


def count_total_targets(filters: AdvancedStatisticsFilters) -> int:
    """Return active target count in the current dataset for coverage stats."""
    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        return 0
    canton = None
    if filters.canton != "all":
        canton = get_current_cantons().filter(abbreviation=filters.canton).first()
        if canton is None:
            return 0
    total = 0
    if filters.target_type in ("all", Game.TargetType.MUNICIPALITY):
        municipalities = (
            get_municipalities_for_canton(canton)
            if canton is not None
            else get_municipalities_for_dataset(dataset_version)
        )
        total += municipalities.count()
    if filters.target_type in ("all", Game.TargetType.VILLAGE):
        villages = (
            get_villages_for_canton(canton)
            if canton is not None
            else get_villages_for_dataset(dataset_version)
        )
        total += villages.count()
    return total


def build_canton_statistics(guesses, target_type: str) -> list[dict]:
    """Return performance grouped by selected target canton."""
    stats_by_canton: dict[str, dict] = {}
    if target_type in ("all", Game.TargetType.MUNICIPALITY):
        add_grouped_canton_rows(
            stats_by_canton,
            guesses.filter(turn__game__target_type=Game.TargetType.MUNICIPALITY),
            "turn__municipality_target_id",
            "turn__municipality_target__canton__abbreviation",
            "turn__municipality_target__canton__name",
        )
    if target_type in ("all", Game.TargetType.VILLAGE):
        add_grouped_canton_rows(
            stats_by_canton,
            guesses.filter(turn__game__target_type=Game.TargetType.VILLAGE),
            "turn__village_target_id",
            "turn__village_target__canton__abbreviation",
            "turn__village_target__canton__name",
        )
    stats = []
    for row in stats_by_canton.values():
        attempts = row["attempts"]
        stats.append(
            {
                **row,
                "average_distance_m": round_or_zero(row["distance_total"] / attempts),
                "average_score": round_or_zero(row["score_total"] / attempts),
                "hit_rate": percentage(row["hits"], attempts),
            }
        )
    return sorted(
        stats,
        key=lambda item: (-item["hit_rate"], -item["average_score"], item["name"]),
    )


def add_grouped_canton_rows(
    stats_by_canton: dict[str, dict],
    guesses,
    target_id_field: str,
    abbreviation_field: str,
    name_field: str,
) -> None:
    """Merge grouped canton aggregate rows into a stats dictionary."""
    rows = guesses.values(abbreviation_field, name_field).annotate(
        attempts=Count("id"),
        average_distance_m=Avg("distance_to_municipality_m"),
        average_score=Avg("score"),
        best_score=Max("score"),
        hits=Count("id", filter=Q(score=EXACT_HIT_SCORE)),
        targets_played=Count(target_id_field, distinct=True),
    )
    for row in rows:
        abbreviation = row[abbreviation_field]
        if not abbreviation:
            continue
        current = stats_by_canton.setdefault(
            abbreviation,
            {
                "abbreviation": abbreviation,
                "attempts": 0,
                "best_score": 0,
                "distance_total": 0,
                "hits": 0,
                "name": row[name_field],
                "score_total": 0,
                "targets_played": 0,
            },
        )
        attempts = row["attempts"] or 0
        current["attempts"] += attempts
        current["best_score"] = max(current["best_score"], row["best_score"] or 0)
        current["distance_total"] += (row["average_distance_m"] or 0) * attempts
        current["hits"] += row["hits"] or 0
        current["score_total"] += (row["average_score"] or 0) * attempts
        current["targets_played"] += row["targets_played"] or 0


def build_target_statistics(guesses, sort: str, target_type: str) -> list[dict]:
    """Return per-target performance rows sorted for the selected view."""
    stats = []
    if target_type in ("all", Game.TargetType.MUNICIPALITY):
        stats.extend(
            target_statistics_for_type(
                guesses.filter(turn__game__target_type=Game.TargetType.MUNICIPALITY),
                Game.TargetType.MUNICIPALITY,
                "turn__municipality_target_id",
                "turn__municipality_target__name",
                "turn__municipality_target__canton__abbreviation",
                "turn__municipality_target__canton__name",
            )
        )
    if target_type in ("all", Game.TargetType.VILLAGE):
        stats.extend(
            target_statistics_for_type(
                guesses.filter(turn__game__target_type=Game.TargetType.VILLAGE),
                Game.TargetType.VILLAGE,
                "turn__village_target_id",
                "turn__village_target__name",
                "turn__village_target__canton__abbreviation",
                "turn__village_target__canton__name",
            )
        )
    sorted_stats = sort_target_statistics(stats, sort)
    for index, item in enumerate(sorted_stats, start=1):
        item["rank"] = index
    return sorted_stats


def target_statistics_for_type(
    guesses,
    target_type: str,
    id_field: str,
    name_field: str,
    canton_field: str,
    canton_name_field: str,
) -> list[dict]:
    """Return per-target aggregate rows for one target type."""
    rows = guesses.values(
        id_field,
        name_field,
        canton_field,
        canton_name_field,
    ).annotate(
        attempts=Count("id"),
        average_distance_m=Avg("distance_to_municipality_m"),
        average_score=Avg("score"),
        best_score=Max("score"),
        hits=Count("id", filter=Q(score=EXACT_HIT_SCORE)),
        last_played_at=Max("guessed_at"),
        near_hits=Count(
            "id",
            filter=Q(score__gte=NEAR_HIT_MIN_SCORE, score__lt=EXACT_HIT_SCORE),
        ),
    )
    stats = []
    for row in rows:
        if row[id_field] is None:
            continue
        attempts = row["attempts"] or 0
        average_score = round_or_zero(row["average_score"])
        hits = row["hits"] or 0
        stats.append(
            {
                "attempts": attempts,
                "average_distance_m": round_or_zero(row["average_distance_m"]),
                "average_score": average_score,
                "best_score": row["best_score"] or 0,
                "canton": row[canton_field],
                "canton_name": row[canton_name_field],
                "hit_rate": percentage(hits, attempts),
                "hits": hits,
                "last_played_at": row["last_played_at"],
                "name": row[name_field],
                "near_hit_rate": percentage(row["near_hits"] or 0, attempts),
                "near_hits": row["near_hits"] or 0,
                "target_id": row[id_field],
                "target_type": target_type,
                "target_type_label": target_type_label(target_type),
            }
        )
    return stats


def sort_target_statistics(stats: list[dict], sort: str) -> list[dict]:
    """Sort per-target rows for ranking or practice views."""
    sorters = {
        "hit_rate_desc": lambda item: (
            -item["hit_rate"],
            -item["attempts"],
            -item["average_score"],
            item["name"],
        ),
        "score_desc": lambda item: (
            -item["average_score"],
            -item["hit_rate"],
            -item["attempts"],
            item["name"],
        ),
        "attempts_desc": lambda item: (
            -item["attempts"],
            -item["average_score"],
            item["name"],
        ),
        "distance_asc": lambda item: (
            item["average_distance_m"],
            -item["average_score"],
            item["name"],
        ),
        "needs_practice": lambda item: (
            item["hit_rate"],
            item["average_score"],
            -item["attempts"],
            item["name"],
        ),
    }
    return sorted(stats, key=sorters.get(sort, sorters["needs_practice"]))


def strongest_targets(target_stats: list[dict]) -> list[dict]:
    """Return the strongest known targets."""
    return sorted(
        target_stats,
        key=lambda item: (
            -item["hit_rate"],
            -item["average_score"],
            -item["attempts"],
            item["name"],
        ),
    )[:5]


def weakest_targets(target_stats: list[dict]) -> list[dict]:
    """Return targets with the most useful practice signal."""
    candidates = [
        item
        for item in target_stats
        if item["hit_rate"] < 100 or item["average_score"] < NEAR_HIT_MIN_SCORE
    ]
    return sorted(
        candidates,
        key=lambda item: (
            item["hit_rate"],
            item["average_score"],
            -item["attempts"],
            item["name"],
        ),
    )[:5]


def build_weekly_trend(guesses) -> list[dict]:
    """Return compact weekly trend data for chart rendering."""
    rows = list(
        guesses.annotate(bucket=TruncWeek("guessed_at"))
        .values("bucket")
        .annotate(
            attempts=Count("id"),
            average_score=Avg("score"),
            hits=Count("id", filter=Q(score=EXACT_HIT_SCORE)),
        )
        .order_by("-bucket")[:12]
    )
    trend = []
    for row in reversed(rows):
        attempts = row["attempts"] or 0
        average_score = round_or_zero(row["average_score"])
        trend.append(
            {
                "attempts": attempts,
                "average_score": average_score,
                "hit_rate": percentage(row["hits"] or 0, attempts),
                "label": row["bucket"].strftime("%d.%m.") if row["bucket"] else "",
                "score_percent": percentage(average_score, EXACT_HIT_SCORE),
            }
        )
    return trend


def percentage(part: int | float, total: int | float) -> int:
    """Return a rounded percentage, using zero for empty denominators."""
    if not total:
        return 0
    return round((part / total) * 100)


def round_or_zero(value) -> int:
    """Round numeric aggregate values, using zero for empty result sets."""
    return round(value) if value is not None else 0


def map_mode_label(mode: dict) -> str:
    """Return a compact label for grouped map-mode statistics."""
    return mode["canton__abbreviation"] or DEFAULT_MAP_LABEL


def target_type_label(target_type: str) -> str:
    """Return a player-facing plural target type label."""
    if target_type == Game.TargetType.VILLAGE:
        return _("Villages")
    return _("Municipalities")
