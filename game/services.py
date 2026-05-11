"""Services for game lifecycle operations."""

import math
import random
from dataclasses import dataclass

from django.contrib.gis.geos import GEOSGeometry, Point
from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from geo.models import Canton, Municipality
from geo.selectors import (
    get_canton_for_dataset_by_abbreviation,
    get_current_dataset_version,
    get_municipalities_for_canton,
    get_municipalities_for_dataset,
)
from tracking.models import GameEvent
from tracking.services import track_event

from .identity import PlayerIdentity
from .models import Game, Guess, Turn
from .scoring import calculate_score
from .selectors import get_active_game_for_player


TURN_COUNT = 5
NEAREST_BOUNDARY_POINT_SQL = """
    ST_AsEWKB(
        ST_ClosestPoint(
            ST_Boundary(target.geom)::geography,
            guess.point::geography
        )::geometry
    )
"""


class NotEnoughMunicipalitiesError(ValueError):
    """Raised when there are not enough active municipalities to start a game."""


class InvalidGameModeError(ValueError):
    """Raised when a requested game mode cannot be used."""


class GuessSubmissionError(ValueError):
    """Raised when a guess cannot be submitted for the requested turn."""


class InvalidGuessCoordinatesError(GuessSubmissionError):
    """Raised when submitted guess coordinates are invalid."""


@dataclass(frozen=True)
class GameScope:
    """Resolved playable map scope for a new game.

    Attributes:
        mode: Game mode value stored on Game.
        canton: Optional canton when the game is scoped to one canton.
        municipalities: QuerySet-like active municipality pool for target sampling.
    """

    mode: str
    canton: Canton | None
    municipalities: QuerySet[Municipality]


def calculate_scoring_max_distance_m_for_dataset(
    dataset_version_id: int,
    *,
    canton_id: int | None = None,
) -> float:
    """Return the scoring distance scale for an active municipality dataset.

    Args:
        dataset_version_id: Dataset version whose active municipalities define
            the playable map scope.
        canton_id: Optional canton restricting the playable map scope.

    Returns:
        The maximum geodesic distance between bounding-box corners in meters.

    Raises:
        NotEnoughMunicipalitiesError: If the dataset has no usable map extent.
    """
    municipality_table = connection.ops.quote_name(Municipality._meta.db_table)
    canton_filter = "AND canton_id = %s" if canton_id is not None else ""
    query = f"""
        WITH bounds AS (
            SELECT ST_Extent(geom) AS box
            FROM {municipality_table}
            WHERE dataset_version_id = %s AND is_active = true {canton_filter}
        ),
        corners AS (
            SELECT
                ST_XMin(box)::float8 AS min_lng,
                ST_YMin(box)::float8 AS min_lat,
                ST_XMax(box)::float8 AS max_lng,
                ST_YMax(box)::float8 AS max_lat
            FROM bounds
            WHERE box IS NOT NULL
        ),
        points AS (
            SELECT * FROM (VALUES
                (
                    'SW',
                    (SELECT ST_SetSRID(ST_MakePoint(min_lng, min_lat), 4326)
                     FROM corners)
                ),
                (
                    'SE',
                    (SELECT ST_SetSRID(ST_MakePoint(max_lng, min_lat), 4326)
                     FROM corners)
                ),
                (
                    'NW',
                    (SELECT ST_SetSRID(ST_MakePoint(min_lng, max_lat), 4326)
                     FROM corners)
                ),
                (
                    'NE',
                    (SELECT ST_SetSRID(ST_MakePoint(max_lng, max_lat), 4326)
                     FROM corners)
                )
            ) AS point(label, geom)
        )
        SELECT MAX(
            ST_Distance(start_point.geom::geography, end_point.geom::geography)
        )
        FROM points AS start_point
        JOIN points AS end_point ON start_point.label < end_point.label
    """
    parameters = [dataset_version_id]
    if canton_id is not None:
        parameters.append(canton_id)
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()

    if row is None or row[0] is None or row[0] <= 0:
        raise NotEnoughMunicipalitiesError(
            "Could not calculate a usable scoring map extent."
        )
    return float(row[0])


@dataclass(frozen=True)
class GuessDistances:
    """Measured distances between a guess point and the target municipality.

    Attributes:
        distance_to_municipality_m: Distance to the municipality polygon in meters.
        distance_to_boundary_m: Distance to the municipality boundary in meters.
        nearest_boundary_point: Boundary point nearest to the guess point.
    """

    distance_to_municipality_m: float
    distance_to_boundary_m: float
    nearest_boundary_point: Point


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
    return start_game_for_player(PlayerIdentity.for_user(user))


def start_game_for_player(
    player: PlayerIdentity,
    *,
    mode: str = Game.Mode.SWITZERLAND,
    canton_abbreviation: str = "",
) -> Game:
    """Return an active game for a player, creating one when needed.

    Args:
        player: User or guest identity that starts or resumes a game.
        mode: Requested game mode.
        canton_abbreviation: Requested canton abbreviation for single-canton mode.

    Returns:
        An active game with five turns.

    Raises:
        NotEnoughMunicipalitiesError: If fewer than five active municipalities exist
            in the current dataset version.
    """
    if not player.can_own_games:
        raise ValueError("Player identity cannot own games.")

    existing_game = get_active_game_for_player(player)
    if existing_game is not None:
        return existing_game

    try:
        with transaction.atomic():
            existing_game = (
                Game.objects.select_for_update()
                .filter(player.owner_query(), status=Game.Status.ACTIVE)
                .order_by("-started_at", "-id")
                .first()
            )
            if existing_game is not None:
                return existing_game

            dataset_version = get_current_dataset_version()
            if dataset_version is None:
                existing_game = get_active_game_for_player(player)
                if existing_game is not None:
                    return existing_game
                raise NotEnoughMunicipalitiesError(
                    f"At least {TURN_COUNT} active municipalities are required to "
                    "start a game."
                )

            game_scope = resolve_game_scope(
                dataset_version=dataset_version,
                mode=mode,
                canton_abbreviation=canton_abbreviation,
            )
            current_municipalities = game_scope.municipalities
            municipality_ids = list(current_municipalities.values_list("id", flat=True))
            if len(municipality_ids) < TURN_COUNT:
                existing_game = get_active_game_for_player(player)
                if existing_game is not None:
                    return existing_game
                raise NotEnoughMunicipalitiesError(
                    f"At least {TURN_COUNT} active municipalities are required to "
                    "start a game."
                )

            scoring_max_distance_m = calculate_scoring_max_distance_m_for_dataset(
                dataset_version.id,
                canton_id=game_scope.canton.id if game_scope.canton else None,
            )
            target_ids = random.SystemRandom().sample(municipality_ids, TURN_COUNT)
            game = Game.objects.create(
                mode=game_scope.mode,
                canton=game_scope.canton,
                scoring_max_distance_m=scoring_max_distance_m,
                **player.model_fields(),
            )
            turns = [
                Turn(game=game, turn_number=turn_number, target_id=target_id)
                for turn_number, target_id in enumerate(target_ids, start=1)
            ]
            Turn.objects.bulk_create(turns)
            persisted_turns = list(game.turns.order_by("turn_number"))
            first_turn = persisted_turns[0]
            track_event(
                game=game,
                event_type=GameEvent.Type.GAME_STARTED,
                **player.model_fields(),
            )
            track_event(
                game=game,
                turn=first_turn,
                event_type=GameEvent.Type.TURN_STARTED,
                payload={"turn_number": first_turn.turn_number},
                **player.model_fields(),
            )
            return game
    except IntegrityError:
        existing_game = get_active_game_for_player(player)
        if existing_game is not None:
            return existing_game
        raise


def resolve_game_scope(
    *,
    dataset_version,
    mode: str,
    canton_abbreviation: str = "",
) -> GameScope:
    """Resolve a requested game mode against the current geodata dataset.

    Args:
        dataset_version: Current geodata dataset version.
        mode: Requested mode value.
        canton_abbreviation: Requested canton abbreviation for canton mode.

    Returns:
        A game scope containing mode, canton, and municipality pool.

    Raises:
        InvalidGameModeError: If the requested mode or canton is invalid.
    """
    normalized_mode = normalize_game_mode(mode)
    if normalized_mode == Game.Mode.SWITZERLAND:
        return GameScope(
            mode=Game.Mode.SWITZERLAND,
            canton=None,
            municipalities=get_municipalities_for_dataset(dataset_version),
        )

    canton = get_canton_for_dataset_by_abbreviation(
        dataset_version,
        canton_abbreviation,
    )
    if canton is None:
        raise InvalidGameModeError("Choose a valid canton.")
    return GameScope(
        mode=Game.Mode.CANTON,
        canton=canton,
        municipalities=get_municipalities_for_canton(canton),
    )


def normalize_game_mode(mode: str) -> str:
    """Normalize and validate a requested game mode."""
    if not mode:
        return Game.Mode.SWITZERLAND
    if mode in Game.Mode.values:
        return mode
    raise InvalidGameModeError("Choose a valid game mode.")


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
    return submit_guess_for_player(
        PlayerIdentity.for_user(user),
        turn_id,
        latitude,
        longitude,
    )


def submit_guess_for_player(
    player: PlayerIdentity,
    turn_id,
    latitude,
    longitude,
) -> GuessSubmissionResult:
    """Submit and score a point guess for a player's current turn.

    Args:
        player: User or guest identity submitting the guess.
        turn_id: Turn being guessed.
        latitude: WGS84 latitude value.
        longitude: WGS84 longitude value.

    Returns:
        The persisted guess, revealed turn, and updated game.

    Raises:
        GuessSubmissionError: If the turn is not guessable by this player.
        InvalidGuessCoordinatesError: If the coordinates are invalid.
    """
    if not player.can_own_games:
        raise GuessSubmissionError("Player identity cannot submit guesses.")

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
        _validate_guessable_turn(player=player, game=game, turn=turn)

        scoring_max_distance_m = _ensure_game_scoring_max_distance_m(
            game=game,
            target_id=turn.target_id,
        )
        distances = _calculate_guess_distances(point=point, target_id=turn.target_id)
        score = calculate_score(
            distances.distance_to_municipality_m,
            scoring_max_distance_m,
        )
        guess = Guess(
            turn=turn,
            point=point,
            distance_to_municipality_m=distances.distance_to_municipality_m,
            distance_to_boundary_m=distances.distance_to_boundary_m,
            nearest_boundary_point=distances.nearest_boundary_point,
            score=score,
            **player.model_fields(),
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
            **player.model_fields(),
        )
        if next_turn is not None:
            track_event(
                game=game,
                turn=next_turn,
                event_type=GameEvent.Type.TURN_STARTED,
                payload={"turn_number": next_turn.turn_number},
                **player.model_fields(),
            )
        else:
            track_event(
                game=game,
                event_type=GameEvent.Type.GAME_FINISHED,
                payload={"total_score": game.total_score},
                **player.model_fields(),
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


def _validate_guessable_turn(*, player: PlayerIdentity, game: Game, turn: Turn) -> None:
    """Validate that a turn may receive a guess from a player.

    Args:
        player: Player submitting the guess.
        game: Locked game containing the turn.
        turn: Locked turn being guessed.

    Raises:
        GuessSubmissionError: If the turn cannot currently be guessed.
    """
    if not player.owns(game):
        raise GuessSubmissionError("Turn does not belong to this player.")
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


def _ensure_game_scoring_max_distance_m(*, game: Game, target_id: int) -> float:
    """Return a game's scoring distance scale, calculating it for legacy games."""
    if (
        game.scoring_max_distance_m is not None
        and math.isfinite(game.scoring_max_distance_m)
        and game.scoring_max_distance_m > 0
    ):
        return game.scoring_max_distance_m

    dataset_version_id = (
        Municipality.objects.only("dataset_version_id")
        .get(pk=target_id)
        .dataset_version_id
    )
    scoring_max_distance_m = calculate_scoring_max_distance_m_for_dataset(
        dataset_version_id,
        canton_id=game.canton_id if game.mode == Game.Mode.CANTON else None,
    )
    game.scoring_max_distance_m = scoring_max_distance_m
    game.save(update_fields=["scoring_max_distance_m"])
    return scoring_max_distance_m


def calculate_nearest_boundary_point(*, point: Point, target_id: int) -> Point:
    """Return the target boundary point nearest to a guess point.

    Args:
        point: Guess point in WGS84 coordinates.
        target_id: Target municipality primary key.

    Returns:
        Nearest boundary point in WGS84 coordinates.

    Raises:
        GuessSubmissionError: If the target municipality cannot be found.
    """
    municipality_table = connection.ops.quote_name(Municipality._meta.db_table)
    point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    query = f"""
        WITH target AS (
            SELECT geom
            FROM {municipality_table}
            WHERE id = %s
        ),
        guess AS (
            SELECT {point_sql} AS point
        )
        SELECT {NEAREST_BOUNDARY_POINT_SQL}
        FROM target, guess
    """
    parameters = [target_id, point.x, point.y]
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()

    if row is None:
        raise GuessSubmissionError("Target municipality does not exist.")

    return GEOSGeometry(memoryview(row[0]))


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
    point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    query = f"""
        WITH target AS (
            SELECT geom
            FROM {municipality_table}
            WHERE id = %s
        ),
        guess AS (
            SELECT {point_sql} AS point
        )
        SELECT
            ST_Distance(target.geom::geography, guess.point::geography),
            ST_Distance(ST_Boundary(target.geom)::geography, guess.point::geography),
            {NEAREST_BOUNDARY_POINT_SQL}
        FROM target, guess
    """
    parameters = [target_id, point.x, point.y]
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()

    if row is None:
        raise GuessSubmissionError("Target municipality does not exist.")

    return GuessDistances(
        distance_to_municipality_m=float(row[0]),
        distance_to_boundary_m=float(row[1]),
        nearest_boundary_point=GEOSGeometry(memoryview(row[2])),
    )
