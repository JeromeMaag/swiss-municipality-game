"""Scoring helpers for municipality guesses."""

import math


MAX_SCORE = 1000
DISTANCE_DECAY_KM = 25


def calculate_score(distance_m: float) -> int:
    """Calculate a guess score from distance to the target municipality.

    Args:
        distance_m: Distance to the target municipality polygon in meters.

    Returns:
        Score between 0 and 1000.

    Raises:
        ValueError: If distance_m is negative or not finite.
    """
    if not math.isfinite(distance_m):
        raise ValueError("Distance must be finite.")
    if distance_m < 0:
        raise ValueError("Distance must not be negative.")

    distance_km = distance_m / 1000
    score = round(MAX_SCORE * math.exp(-distance_km / DISTANCE_DECAY_KM))
    return max(0, min(MAX_SCORE, score))
