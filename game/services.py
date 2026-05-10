"""Services for game lifecycle operations."""

import math
import random
from dataclasses import dataclass

from django.contrib.gis.geos import Point
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from geo.models import Municipality
from geo.selectors import get_current_municipalities
from tracking.models import GameEvent
from tracking.services import track_event

from .models import Game, Guess, Turn
from .scoring import calculate_score
from .selectors import get_active_game


TURN_COUNT = 5


class NotEnoughMunicipalitiesError(ValueError):
    """Raised when there are not enough active municipalities to start a game."""


class GuessSubmissionError(ValueError):
    """Raised when a guess cannot be submitted for the requested turn."""


class InvalidGuessCoordinatesError(GuessSubmissionError):
    """Raised when submitted guess coordinates are invalid."""


@dataclass(frozen=True)
class GuessDistances:
    """Measured distances between a guess point and the target municipality.

    Attributes:
        distance_to_municipality_m: Distance to the municipality polygon in meters.
        distance_to_boundary_m: Distance to the municipality boundary in meters.
    """

    distance_to_municipality_m: float
    distance_to_boundary_m: float


@dataclass(frozen=True)
class GuessSubmissionResult:
    """Result of a persisted guess submission.

    Attributes:
        guess: Persisted guess object.
        game: Updated game object.
        turn: Revealed turn object.
    """

    guess: Guess
    game: Game
    turn: Turn


def start_game(user) -> Game:
    """Return an active game for a user, creating one when needed.

    Args:
        user: User who starts or resumes a game.

    Returns:
        An active game with five turns.

    Raises:
        NotEnoughMunicipalitiesError: If fewer than five active municipalities exist
            in the current dataset version.
    """
    existing_game = get_active_game(user)
    if existing_game is not None:
        return existing_game

    try:
        with transaction.atomic():
            existing_game = (
                Game.objects.select_for_update()
                .filter(user=user, status=Game.Status.ACTIVE)
                .order_by("-started_at", "-id")
                .first()
            )
            if existing_game is not None:
                return existing_game

            municipality_ids = list(
                get_current_municipalities().values_list("id", flat=True)
            )
            if len(municipality_ids) < TURN_COUNT:
                existing_game = get_active_game(user)
                if existing_game is not None:
                    return existing_game
                raise NotEnoughMunicipalitiesError(
                    f"At least {TURN_COUNT} active municipalities are required to "
                    "start a game."
                )

            target_ids = random.SystemRandom().sample(municipality_ids, TURN_COUNT)
            game = Game.objects.create(user=user)
            turns = [
                Turn(game=game, turn_number=turn_number, target_id=target_id)
                for turn_number, target_id in enumerate(target_ids, start=1)
            ]
            Turn.objects.bulk_create(turns)
            persisted_turns = list(game.turns.order_by("turn_number"))
            first_turn = persisted_turns[0]
            track_event(
                user=user,
                game=game,
                event_type=GameEvent.Type.GAME_STARTED,
            )
            track_event(
                user=user,
                game=game,
                turn=first_turn,
                event_type=GameEvent.Type.TURN_STARTED,
                payload={"turn_number": first_turn.turn_number},
            )
            return game
    except IntegrityError:
        existing_game = get_active_game(user)
        if existing_game is not None:
            return existing_game
        raise


def submit_guess(user, turn_id, latitude, longitude) -> GuessSubmissionResult:
    """Submit and score a point guess for the current turn.

    Args:
        user: User submitting the guess.
        turn_id: Turn being guessed.
        latitude: WGS84 latitude value.
        longitude: WGS84 longitude value.

    Returns:
        The persisted guess, revealed turn, and updated game.

    Raises:
        GuessSubmissionError: If the turn is not guessable by this user.
        InvalidGuessCoordinatesError: If the coordinates are invalid.
    """
    latitude = _normalize_coordinate(
        latitude,
        name="Latitude",
        minimum=-90,
        maximum=90,
    )
    longitude = _normalize_coordinate(
        longitude,
        name="Longitude",
        minimum=-180,
        maximum=180,
    )
    turn_pk = _normalize_turn_id(turn_id)
    point = Point(longitude, latitude, srid=4326)

    with transaction.atomic():
        try:
            turn = (
                Turn.objects.select_for_update()
                .select_related("game")
                .get(pk=turn_pk)
            )
        except Turn.DoesNotExist as error:
            raise GuessSubmissionError("Turn does not exist.") from error

        game = Game.objects.select_for_update().get(pk=turn.game_id)
        turn.game = game
        _validate_guessable_turn(user=user, game=game, turn=turn)

        distances = _calculate_guess_distances(point=point, target_id=turn.target_id)
        score = calculate_score(distances.distance_to_municipality_m)
        guess = Guess(
            turn=turn,
            user=user,
            point=point,
            distance_to_municipality_m=distances.distance_to_municipality_m,
            distance_to_boundary_m=distances.distance_to_boundary_m,
            score=score,
        )
        guess.full_clean()
        guess.save()

        turn.revealed_at = timezone.now()
        turn.save(update_fields=["revealed_at"])

        game.total_score += score
        next_turn = (
            game.turns.filter(revealed_at__isnull=True)
            .order_by("turn_number")
            .first()
        )
        update_fields = ["total_score"]
        if next_turn is None:
            game.status = Game.Status.FINISHED
            game.finished_at = timezone.now()
            update_fields.extend(["status", "finished_at"])
        game.save(update_fields=update_fields)

        track_event(
            user=user,
            game=game,
            turn=turn,
            event_type=GameEvent.Type.GUESS_CONFIRMED,
            payload={
                "turn_number": turn.turn_number,
                "latitude": latitude,
                "longitude": longitude,
                "distance_to_municipality_m": distances.distance_to_municipality_m,
                "distance_to_boundary_m": distances.distance_to_boundary_m,
                "score": score,
            },
        )
        if next_turn is not None:
            track_event(
                user=user,
                game=game,
                turn=next_turn,
                event_type=GameEvent.Type.TURN_STARTED,
                payload={"turn_number": next_turn.turn_number},
            )
        else:
            track_event(
                user=user,
                game=game,
                event_type=GameEvent.Type.GAME_FINISHED,
                payload={"total_score": game.total_score},
            )

        return GuessSubmissionResult(guess=guess, game=game, turn=turn)


def _normalize_coordinate(value, *, name: str, minimum: float, maximum: float) -> float:
    """Normalize and validate one coordinate value.

    Args:
        value: Raw coordinate value.
        name: Human-readable coordinate name.
        minimum: Inclusive lower bound.
        maximum: Inclusive upper bound.

    Returns:
        A finite coordinate as float.

    Raises:
        InvalidGuessCoordinatesError: If the coordinate is not a finite number
            within the allowed bounds.
    """
    try:
        coordinate = float(value)
    except (TypeError, ValueError) as error:
        raise InvalidGuessCoordinatesError(f"{name} must be a number.") from error

    if not math.isfinite(coordinate):
        raise InvalidGuessCoordinatesError(f"{name} must be finite.")
    if coordinate < minimum or coordinate > maximum:
        raise InvalidGuessCoordinatesError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return coordinate


def _normalize_turn_id(value) -> int:
    """Normalize and validate a submitted turn identifier.

    Args:
        value: Raw turn identifier.

    Returns:
        A positive integer turn primary key.

    Raises:
        GuessSubmissionError: If the turn identifier is not a positive integer.
    """
    try:
        turn_id = int(value)
    except (TypeError, ValueError) as error:
        raise GuessSubmissionError("Turn is invalid.") from error

    if turn_id < 1:
        raise GuessSubmissionError("Turn is invalid.")
    return turn_id


def _validate_guessable_turn(*, user, game: Game, turn: Turn) -> None:
    """Validate that a turn may receive a guess from a user.

    Args:
        user: User submitting the guess.
        game: Locked game containing the turn.
        turn: Locked turn being guessed.

    Raises:
        GuessSubmissionError: If the turn cannot currently be guessed.
    """
    if game.user_id != user.id:
        raise GuessSubmissionError("Turn does not belong to this user.")
    if game.status != Game.Status.ACTIVE:
        raise GuessSubmissionError("Game is not active.")
    if turn.revealed_at is not None or Guess.objects.filter(turn=turn).exists():
        raise GuessSubmissionError("Turn has already been guessed.")

    current_turn_id = (
        game.turns.filter(revealed_at__isnull=True)
        .order_by("turn_number")
        .values_list("id", flat=True)
        .first()
    )
    if current_turn_id != turn.id:
        raise GuessSubmissionError("Turn is not the current turn.")


def _calculate_guess_distances(*, point: Point, target_id: int) -> GuessDistances:
    """Calculate geodesic guess distances against a municipality polygon.

    Args:
        point: Guess point in WGS84 coordinates.
        target_id: Target municipality primary key.

    Returns:
        Distances to the municipality polygon and boundary in meters.

    Raises:
        GuessSubmissionError: If the target municipality cannot be found.
    """
    municipality_table = connection.ops.quote_name(Municipality._meta.db_table)
    point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"
    query = f"""
        SELECT
            ST_Distance(geom::geography, {point_sql}),
            ST_Distance(ST_Boundary(geom)::geography, {point_sql})
        FROM {municipality_table}
        WHERE id = %s
    """
    parameters = [point.x, point.y, point.x, point.y, target_id]
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()

    if row is None:
        raise GuessSubmissionError("Target municipality does not exist.")

    return GuessDistances(
        distance_to_municipality_m=float(row[0]),
        distance_to_boundary_m=float(row[1]),
    )
