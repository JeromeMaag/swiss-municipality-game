"""Services for game lifecycle operations."""

import math
import random
from dataclasses import dataclass

from django.contrib.gis.geos import GEOSGeometry, Point
from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy

from geo.models import Canton, Municipality, Village
from geo.selectors import (
    get_canton_for_dataset_by_abbreviation,
    get_current_dataset_version,
    get_municipalities_for_canton,
    get_municipalities_for_dataset,
    get_villages_for_canton,
    get_villages_for_dataset,
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


class NotEnoughTargetsError(ValueError):
    """Raised when there are not enough active targets to start a game."""


NotEnoughMunicipalitiesError = NotEnoughTargetsError


class InvalidGameModeError(ValueError):
    """Raised when a requested game mode cannot be used."""


class InvalidGameTargetTypeError(ValueError):
    """Raised when a requested game target type cannot be used."""


class GuessSubmissionError(ValueError):
    """Raised when a guess cannot be submitted for the requested turn."""


class InvalidGuessCoordinatesError(GuessSubmissionError):
    """Raised when submitted guess coordinates are invalid."""


@dataclass(frozen=True)
class GameScope:
    """Resolved playable map scope for a new game.

    Attributes:
        mode: Game mode value stored on Game.
        target_type: Game target type value stored on Game.
        canton: Optional canton when the game is scoped to one canton.
        targets: QuerySet-like active target pool for sampling.
    """

    mode: str
    target_type: str
    canton: Canton | None
    targets: QuerySet[Municipality] | QuerySet[Village]


@dataclass(frozen=True)
class TargetTypeConfig:
    """Database mapping for one game target type.

    Attributes:
        target_type: Game target type value.
        model: Geo model used by this target type.
        turn_field_id: Turn foreign-key id field storing this target.
        display_name: Translatable human-readable plural target name.
    """

    target_type: str
    model: type[Municipality] | type[Village]
    turn_field_id: str
    display_name: str


MUNICIPALITY_TARGET_CONFIG = TargetTypeConfig(
    target_type=Game.TargetType.MUNICIPALITY,
    model=Municipality,
    turn_field_id="municipality_target_id",
    display_name=gettext_lazy("municipalities"),
)
VILLAGE_TARGET_CONFIG = TargetTypeConfig(
    target_type=Game.TargetType.VILLAGE,
    model=Village,
    turn_field_id="village_target_id",
    display_name=gettext_lazy("villages"),
)


def calculate_scoring_max_distance_m_for_dataset(
    dataset_version_id: int,
    *,
    canton_id: int | None = None,
    target_type: str = Game.TargetType.MUNICIPALITY,
) -> float:
    """Return the scoring distance scale for an active target dataset.

    Args:
        dataset_version_id: Dataset version whose active targets define the
            playable map scope.
        canton_id: Optional canton restricting the playable map scope.
        target_type: Game target type whose geometry defines the scope.

    Returns:
        The maximum geodesic distance between bounding-box corners in meters.

    Raises:
        NotEnoughTargetsError: If the dataset has no usable map extent.
    """
    target_config = target_type_config(target_type)
    target_table = connection.ops.quote_name(target_config.model._meta.db_table)
    canton_filter = "AND canton_id = %s" if canton_id is not None else ""
    query = f"""
        WITH bounds AS (
            SELECT ST_Extent(geom) AS box
            FROM {target_table}
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
        raise NotEnoughTargetsError(
            _("Could not calculate a usable scoring map extent.")
        )
    return float(row[0])


@dataclass(frozen=True)
class GuessDistances:
    """Measured distances between a guess point and the target polygon.

    Attributes:
        distance_to_municipality_m: Distance to the target polygon in meters.
            The field name is kept for the existing Guess schema.
        distance_to_boundary_m: Distance to the target boundary in meters.
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
        NotEnoughTargetsError: If fewer than five active targets exist in
            the current dataset version.
    """
    return start_game_for_player(PlayerIdentity.for_user(user))


def start_game_for_player(
    player: PlayerIdentity,
    *,
    mode: str = Game.Mode.SWITZERLAND,
    canton_abbreviation: str = "",
    target_type: str = Game.TargetType.MUNICIPALITY,
) -> Game:
    """Return an active game for a player, creating one when needed.

    Args:
        player: User or guest identity that starts or resumes a game.
        mode: Requested game mode.
        canton_abbreviation: Requested canton abbreviation for single-canton mode.
        target_type: Requested target type.

    Returns:
        An active game with five turns.

    Raises:
        NotEnoughTargetsError: If fewer than five active targets exist in
            the current dataset version.
    """
    if not player.can_own_games:
        raise ValueError(_("Player identity cannot own games."))

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
                target_config = target_type_config(target_type)
                existing_game = get_active_game_for_player(player)
                if existing_game is not None:
                    return existing_game
                raise NotEnoughTargetsError(
                    _(
                        "At least %(count)s active %(targets)s are required "
                        "to start a game."
                    )
                    % {"count": TURN_COUNT, "targets": target_config.display_name}
                )

            game_scope = resolve_game_scope(
                dataset_version=dataset_version,
                mode=mode,
                canton_abbreviation=canton_abbreviation,
                target_type=target_type,
            )
            target_config = target_type_config(game_scope.target_type)
            target_count = game_scope.targets.count()
            if target_count < TURN_COUNT:
                existing_game = get_active_game_for_player(player)
                if existing_game is not None:
                    return existing_game
                raise NotEnoughTargetsError(
                    _(
                        "At least %(count)s active %(targets)s are required "
                        "to start a game."
                    )
                    % {"count": TURN_COUNT, "targets": target_config.display_name}
                )

            scoring_max_distance_m = calculate_scoring_max_distance_m_for_dataset(
                dataset_version.id,
                canton_id=game_scope.canton.id if game_scope.canton else None,
                target_type=game_scope.target_type,
            )
            selected_target_ids = sample_target_ids(
                game_scope.targets,
                target_count=target_count,
            )
            game = Game.objects.create(
                mode=game_scope.mode,
                target_type=game_scope.target_type,
                canton=game_scope.canton,
                scoring_max_distance_m=scoring_max_distance_m,
                **player.model_fields(),
            )
            turns = [
                Turn(
                    game=game,
                    turn_number=turn_number,
                    **{target_config.turn_field_id: target_id},
                )
                for turn_number, target_id in enumerate(
                    selected_target_ids,
                    start=1,
                )
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


def sample_target_ids(
    targets: QuerySet[Municipality] | QuerySet[Village],
    *,
    target_count: int,
) -> list[int]:
    """Return a random fixed-size sample with bounded query count."""
    randomizer = random.SystemRandom()
    offsets = sorted(randomizer.sample(range(target_count), TURN_COUNT))
    ordered_targets = targets.order_by("id")
    window_start = offsets[0]
    window_size = offsets[-1] - window_start + 1
    window_ids = list(
        ordered_targets.values_list("id", flat=True)[
            window_start : window_start + window_size
        ]
    )
    target_ids = [window_ids[offset - window_start] for offset in offsets]
    if len(target_ids) != TURN_COUNT:
        raise NotEnoughTargetsError(
            _("At least %(count)s active targets are required to start a game.")
            % {"count": TURN_COUNT}
        )
    return target_ids


def resolve_game_scope(
    *,
    dataset_version,
    mode: str,
    canton_abbreviation: str = "",
    target_type: str = Game.TargetType.MUNICIPALITY,
) -> GameScope:
    """Resolve a requested game mode against the current geodata dataset.

    Args:
        dataset_version: Current geodata dataset version.
        mode: Requested mode value.
        canton_abbreviation: Requested canton abbreviation for canton mode.
        target_type: Requested target type value.

    Returns:
        A game scope containing mode, target type, canton, and target pool.

    Raises:
        InvalidGameModeError: If the requested mode or canton is invalid.
        InvalidGameTargetTypeError: If the requested target type is invalid.
    """
    normalized_mode = normalize_game_mode(mode)
    normalized_target_type = normalize_game_target_type(target_type)
    if normalized_mode == Game.Mode.SWITZERLAND:
        return GameScope(
            mode=Game.Mode.SWITZERLAND,
            target_type=normalized_target_type,
            canton=None,
            targets=targets_for_dataset(
                normalized_target_type,
                dataset_version,
            ),
        )

    canton = get_canton_for_dataset_by_abbreviation(
        dataset_version,
        canton_abbreviation,
    )
    if canton is None:
        raise InvalidGameModeError(_("Choose a valid canton."))
    return GameScope(
        mode=Game.Mode.CANTON,
        target_type=normalized_target_type,
        canton=canton,
        targets=targets_for_canton(normalized_target_type, canton),
    )


def normalize_game_mode(mode: str) -> str:
    """Normalize and validate a requested game mode."""
    if not mode:
        return Game.Mode.SWITZERLAND
    if mode in Game.Mode.values:
        return mode
    raise InvalidGameModeError(_("Choose a valid game mode."))


def normalize_game_target_type(target_type: str) -> str:
    """Normalize and validate a requested target type."""
    if not target_type:
        return Game.TargetType.MUNICIPALITY
    if target_type in Game.TargetType.values:
        return target_type
    raise InvalidGameTargetTypeError(_("Choose a valid target type."))


def target_type_config(target_type: str) -> TargetTypeConfig:
    """Return database mapping for a target type."""
    normalized_target_type = normalize_game_target_type(target_type)
    if normalized_target_type == Game.TargetType.VILLAGE:
        return VILLAGE_TARGET_CONFIG
    return MUNICIPALITY_TARGET_CONFIG


def targets_for_dataset(
    target_type: str,
    dataset_version,
) -> QuerySet[Municipality] | QuerySet[Village]:
    """Return active game targets for one dataset version."""
    normalized_target_type = normalize_game_target_type(target_type)
    if normalized_target_type == Game.TargetType.VILLAGE:
        return get_villages_for_dataset(dataset_version)
    return get_municipalities_for_dataset(dataset_version)


def targets_for_canton(
    target_type: str,
    canton: Canton,
) -> QuerySet[Municipality] | QuerySet[Village]:
    """Return active game targets for one canton."""
    normalized_target_type = normalize_game_target_type(target_type)
    if normalized_target_type == Game.TargetType.VILLAGE:
        return get_villages_for_canton(canton)
    return get_municipalities_for_canton(canton)


def target_id_for_turn(turn: Turn) -> int:
    """Return the concrete target id for a turn's game target type."""
    target_config = target_type_config(turn.game.target_type)
    target_id = getattr(turn, target_config.turn_field_id)
    if target_id is None:
        raise GuessSubmissionError(_("Turn target does not exist."))
    return target_id


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
        raise GuessSubmissionError(_("Player identity cannot submit guesses."))

    latitude = _normalize_coordinate(
        latitude,
        name=_("Latitude"),
        minimum=-90,
        maximum=90,
    )
    longitude = _normalize_coordinate(
        longitude,
        name=_("Longitude"),
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
            raise GuessSubmissionError(_("Turn does not exist.")) from error

        game = Game.objects.select_for_update().get(pk=turn.game_id)
        turn.game = game
        _validate_guessable_turn(player=player, game=game, turn=turn)
        target_id = target_id_for_turn(turn)

        scoring_max_distance_m = _ensure_game_scoring_max_distance_m(
            game=game,
            target_id=target_id,
        )
        distances = _calculate_guess_distances(
            point=point,
            target_id=target_id,
            target_type=game.target_type,
        )
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
        raise InvalidGuessCoordinatesError(
            _("%(name)s must be a number.") % {"name": name}
        ) from error

    if not math.isfinite(coordinate):
        raise InvalidGuessCoordinatesError(
            _("%(name)s must be finite.") % {"name": name}
        )
    if coordinate < minimum or coordinate > maximum:
        raise InvalidGuessCoordinatesError(
            _("%(name)s must be between %(minimum)s and %(maximum)s.")
            % {"name": name, "minimum": minimum, "maximum": maximum}
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
        raise GuessSubmissionError(_("Turn is invalid.")) from error

    if turn_id < 1:
        raise GuessSubmissionError(_("Turn is invalid."))
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
        raise GuessSubmissionError(_("Turn does not belong to this player."))
    if game.status != Game.Status.ACTIVE:
        raise GuessSubmissionError(_("Game is not active."))
    if turn.revealed_at is not None or Guess.objects.filter(turn=turn).exists():
        raise GuessSubmissionError(_("Turn has already been guessed."))

    current_turn_id = (
        game.turns.filter(revealed_at__isnull=True)
        .order_by("turn_number")
        .values_list("id", flat=True)
        .first()
    )
    if current_turn_id != turn.id:
        raise GuessSubmissionError(_("Turn is not the current turn."))


def _ensure_game_scoring_max_distance_m(*, game: Game, target_id: int) -> float:
    """Return a game's scoring distance scale, calculating it for legacy games."""
    if (
        game.scoring_max_distance_m is not None
        and math.isfinite(game.scoring_max_distance_m)
        and game.scoring_max_distance_m > 0
    ):
        return game.scoring_max_distance_m

    target_config = target_type_config(game.target_type)
    dataset_version_id = (
        target_config.model.objects.only("dataset_version_id")
        .get(pk=target_id)
        .dataset_version_id
    )
    scoring_max_distance_m = calculate_scoring_max_distance_m_for_dataset(
        dataset_version_id,
        canton_id=game.canton_id if game.mode == Game.Mode.CANTON else None,
        target_type=game.target_type,
    )
    game.scoring_max_distance_m = scoring_max_distance_m
    game.save(update_fields=["scoring_max_distance_m"])
    return scoring_max_distance_m


def calculate_nearest_boundary_point(
    *,
    point: Point,
    target_id: int,
    target_type: str = Game.TargetType.MUNICIPALITY,
) -> Point:
    """Return the target boundary point nearest to a guess point.

    Args:
        point: Guess point in WGS84 coordinates.
        target_id: Target primary key.
        target_type: Game target type.

    Returns:
        Nearest boundary point in WGS84 coordinates.

    Raises:
        GuessSubmissionError: If the target cannot be found.
    """
    target_config = target_type_config(target_type)
    target_table = connection.ops.quote_name(target_config.model._meta.db_table)
    point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    query = f"""
        WITH target AS (
            SELECT geom
            FROM {target_table}
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
        raise GuessSubmissionError(_("Target does not exist."))

    return GEOSGeometry(memoryview(row[0]))


def _calculate_guess_distances(
    *,
    point: Point,
    target_id: int,
    target_type: str = Game.TargetType.MUNICIPALITY,
) -> GuessDistances:
    """Calculate geodesic guess distances against a target polygon.

    Args:
        point: Guess point in WGS84 coordinates.
        target_id: Target primary key.
        target_type: Game target type.

    Returns:
        Distances to the target polygon and boundary in meters.

    Raises:
        GuessSubmissionError: If the target cannot be found.
    """
    target_config = target_type_config(target_type)
    target_table = connection.ops.quote_name(target_config.model._meta.db_table)
    point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    query = f"""
        WITH target AS (
            SELECT geom
            FROM {target_table}
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
        raise GuessSubmissionError(_("Target does not exist."))

    return GuessDistances(
        distance_to_municipality_m=float(row[0]),
        distance_to_boundary_m=float(row[1]),
        nearest_boundary_point=GEOSGeometry(memoryview(row[2])),
    )
