"""Scoring helpers for municipality guesses."""

import math


MAX_SCORE = 1000
SCORING_DISTANCE_DIVISOR = 20


def calculate_score(distance_m: float, map_max_distance_m: float) -> int:
    """Calculate a guess score from distance to the target municipality.

    Args:
        distance_m: Distance to the target municipality polygon in meters.
        map_max_distance_m: Maximum geodesic distance across the game map scope.

    Returns:
        Score between 0 and 1000.

    Raises:
        ValueError: If any input distance is negative or not finite.
    """
    if not math.isfinite(distance_m):
        raise ValueError("Distance must be finite.")
    if distance_m < 0:
        raise ValueError("Distance must not be negative.")
    if not math.isfinite(map_max_distance_m):
        raise ValueError("Map maximum distance must be finite.")
    if map_max_distance_m <= 0:
        raise ValueError("Map maximum distance must be positive.")

    if distance_m == 0:
        return MAX_SCORE

    decay_m = map_max_distance_m / SCORING_DISTANCE_DIVISOR
    score = round(MAX_SCORE * math.exp(-distance_m / decay_m))
    return max(0, min(MAX_SCORE - 1, score))
