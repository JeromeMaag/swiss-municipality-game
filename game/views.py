"""Views for game pages."""

import json

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from geo.constants import MUNICIPALITY_LABEL_ACCESS_SESSION_KEY
from geo.models import Municipality
from tracking.models import GameEvent
from tracking.services import track_event

from .identity import get_player_identity
from .models import Game, Guess, Turn
from .selectors import (
    get_active_game_for_player,
    get_finished_game_summary_for_player,
)
from .services import (
    TURN_COUNT,
    GuessSubmissionError,
    NotEnoughMunicipalitiesError,
    start_game_for_player,
    submit_guess_for_player,
)


CLIENT_TRACKING_EVENT_TYPES = frozenset(
    {
        GameEvent.Type.MAP_CLICKED,
        GameEvent.Type.REVEAL_SHOWN,
        GameEvent.Type.NEXT_TURN_CLICKED,
    }
)
MAX_TRACKING_REQUEST_BYTES = 4096


@require_GET
def index(request):
    """Render the current game entry page.

    Args:
        request: The incoming HTTP request.

    Returns:
        A rendered game page for the active game or start form.
    """
    active_game = None
    last_guess = None
    player = get_player_identity(request)
    if player.can_own_games:
        active_game = get_active_game_for_player(player)
        last_guess = get_last_guess_result(request, player=player)
    if active_game is None and last_guess is not None:
        active_game = last_guess.turn.game
    elif (
        active_game is not None
        and last_guess is not None
        and last_guess.turn.game_id != active_game.id
    ):
        last_guess = None
    return render_game_index(
        request,
        active_game=active_game,
        last_guess=last_guess,
    )


@require_POST
def start(request):
    """Start or resume an active game for the current player.

    Args:
        request: The incoming HTTP request.

    Returns:
        A redirect to the game index, or a validation response if setup is blocked.
    """
    player = get_player_identity(request, create_session=True)
    try:
        start_game_for_player(player)
    except NotEnoughMunicipalitiesError as error:
        return render_game_index(request, error=str(error), status=400)
    return redirect("game:index")


@require_POST
def guess(request):
    """Submit a guess for the current player's game turn.

    Args:
        request: The incoming HTTP request.

    Returns:
        A redirect to the game index, or a validation response when the guess is
        rejected.
    """
    player = get_player_identity(request)
    try:
        result = submit_guess_for_player(
            player=player,
            turn_id=request.POST.get("turn_id"),
            latitude=request.POST.get("latitude"),
            longitude=request.POST.get("longitude"),
        )
    except GuessSubmissionError as error:
        active_game = (
            get_active_game_for_player(player) if player.can_own_games else None
        )
        return render_game_index(
            request,
            active_game=active_game,
            error=str(error),
            status=400,
        )
    request.session["last_guess_id"] = result.guess.id
    return redirect("game:index")


@require_POST
def track_turn_event(request, turn_id: int):
    """Persist a client-side tracking event for a game turn.

    Args:
        request: The incoming HTTP request.
        turn_id: Turn primary key from the tracking URL.

    Returns:
        An empty successful response when the event is stored, or a JSON error
        response for invalid tracking input.

    Raises:
        Http404: If the turn does not belong to the current player.
    """
    player = get_player_identity(request)
    if not player.can_own_games:
        raise Http404("Turn not found.")

    try:
        event_type, payload = parse_tracking_request(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)

    try:
        turn = Turn.objects.select_related("game").get(
            player.owner_query("game"),
            pk=turn_id,
        )
    except Turn.DoesNotExist as error:
        raise Http404("Turn not found.") from error

    try:
        validate_tracking_event_state(event_type=event_type, turn=turn)
        track_event(
            game=turn.game,
            turn=turn,
            event_type=event_type,
            payload=payload,
            **player.model_fields(),
        )
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    except ValidationError as error:
        error_detail = getattr(error, "message_dict", {"errors": error.messages})
        return JsonResponse({"error": error_detail}, status=400)

    return HttpResponse(status=204)


def validate_tracking_event_state(*, event_type: str, turn: Turn) -> None:
    """Validate that a client event matches the current turn state.

    Args:
        event_type: Client-side tracking event type.
        turn: Turn associated with the event.

    Raises:
        ValueError: If the event is not valid for the turn's current state.
    """
    if event_type == GameEvent.Type.MAP_CLICKED:
        current_turn_id = (
            turn.game.turns.filter(revealed_at__isnull=True)
            .order_by("turn_number")
            .values_list("id", flat=True)
            .first()
        )
        if turn.game.status != Game.Status.ACTIVE or current_turn_id != turn.id:
            raise ValueError("Tracking event is not valid for this turn state.")
        return

    if event_type == GameEvent.Type.REVEAL_SHOWN:
        if turn.revealed_at is None or not is_latest_revealed_turn(turn):
            raise ValueError("Tracking event is not valid for this turn state.")
        return

    if event_type == GameEvent.Type.NEXT_TURN_CLICKED:
        next_turn_exists = turn.game.turns.filter(
            revealed_at__isnull=True,
            turn_number__gt=turn.turn_number,
        ).exists()
        if (
            turn.revealed_at is None
            or not is_latest_revealed_turn(turn)
            or turn.game.status != Game.Status.ACTIVE
            or not next_turn_exists
        ):
            raise ValueError("Tracking event is not valid for this turn state.")


def is_latest_revealed_turn(turn: Turn) -> bool:
    """Return whether a turn is the latest revealed turn in its game.

    Args:
        turn: Turn to compare against the game state.

    Returns:
        True when no later turn has already been revealed.
    """
    return not turn.game.turns.filter(
        revealed_at__isnull=False,
        turn_number__gt=turn.turn_number,
    ).exists()


@require_GET
def summary(request, game_id: int):
    """Render the summary for a finished game owned by the current player.

    Args:
        request: The incoming HTTP request.
        game_id: Finished game primary key.

    Returns:
        A rendered summary page for the finished game.

    Raises:
        Http404: If the game is not finished or does not belong to the player.
    """
    player = get_player_identity(request)
    if not player.can_own_games:
        raise Http404("Game summary not found.")
    game = get_finished_game_summary_for_player(player, game_id)
    if game is None:
        raise Http404("Game summary not found.")
    return render(
        request,
        "game/summary.html",
        {
            "game": game,
            "summary_reveals": build_summary_reveals(game),
            "turn_count": TURN_COUNT,
        },
    )


def build_summary_reveals(game: Game) -> list[dict]:
    """Return map reveal data for every guessed turn in a finished game.

    Args:
        game: Finished game with turns and guesses prefetched. Ordering is
            enforced in memory so callers do not need to re-query.

    Returns:
        JSON-serializable reveal data for the summary map.
    """
    reveals = []
    for turn in sorted(game.turns.all(), key=lambda turn: turn.turn_number):
        try:
            guess = turn.guess
        except Guess.DoesNotExist:
            continue
        reveals.append(
            {
                "distance": guess.distance_to_municipality_m,
                "lat": guess.point.y,
                "lng": guess.point.x,
                "score": guess.score,
                "targetId": turn.target_id,
                "turnNumber": turn.turn_number,
            }
        )
    return reveals


def parse_tracking_request(request) -> tuple[str, dict]:
    """Parse and validate a client tracking request body.

    Args:
        request: The incoming HTTP request.

    Returns:
        A tuple of event type and JSON payload.

    Raises:
        ValueError: If the request body is invalid or the event type is not
            allowed for client-side tracking.
    """
    body = get_tracking_request_body(request)
    if len(body) > MAX_TRACKING_REQUEST_BYTES:
        raise ValueError("Tracking payload is too large.")
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Tracking payload must be valid JSON.") from error

    if not isinstance(data, dict):
        raise ValueError("Tracking payload must be a JSON object.")

    event_type = data.get("event_type")
    if event_type not in CLIENT_TRACKING_EVENT_TYPES:
        raise ValueError("Tracking event type is not allowed.")

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("Tracking event payload must be a JSON object.")

    return event_type, payload


def get_tracking_request_body(request) -> bytes:
    """Return a tracking request body after checking the declared size.

    Args:
        request: The incoming HTTP request.

    Returns:
        The raw request body.

    Raises:
        ValueError: If the declared request body size is missing, invalid, or too
            large.
    """
    content_length = request.META.get("CONTENT_LENGTH")
    if not content_length:
        raise ValueError("Tracking content length is required.")
    try:
        declared_length = int(content_length)
    except ValueError:
        raise ValueError("Tracking content length is invalid.") from None
    if declared_length < 0:
        raise ValueError("Tracking content length is invalid.")
    if declared_length > MAX_TRACKING_REQUEST_BYTES:
        raise ValueError("Tracking payload is too large.")
    return request.body


def get_last_guess_result(request, player=None) -> Guess | None:
    """Return the one-time result guess stored in the session.

    Args:
        request: The incoming HTTP request.
        player: Optional precomputed player identity.

    Returns:
        The submitted guess result, or None when no valid result is pending.
    """
    guess_id = request.session.pop("last_guess_id", None)
    if guess_id is None:
        return None
    try:
        guess_pk = int(guess_id)
    except (TypeError, ValueError):
        return None
    if guess_pk < 1:
        return None

    if player is None:
        player = get_player_identity(request)
    if not player.can_own_games:
        return None

    return (
        Guess.objects.select_related("turn__game", "turn__target__canton")
        .only(
            "id",
            "user",
            "session_key",
            "point",
            "distance_to_municipality_m",
            "distance_to_boundary_m",
            "score",
            "turn__id",
            "turn__turn_number",
            "turn__game__id",
            "turn__game__status",
            "turn__game__total_score",
            "turn__game__user",
            "turn__game__session_key",
            "turn__target__name",
            "turn__target__population",
            "turn__target__canton__abbreviation",
            "turn__target__canton__name",
        )
        .defer(
            "turn__target__geom",
            "turn__target__geom_simplified",
            "turn__target__label_point",
            "turn__target__canton__geom",
            "turn__target__canton__geom_simplified",
            "turn__target__canton__label_point",
        )
        .filter(player.owner_query(), pk=guess_pk)
        .first()
    )


def render_game_index(
    request,
    active_game=None,
    last_guess=None,
    error: str = "",
    status: int = 200,
):
    """Render the game index template.

    Args:
        request: The incoming HTTP request.
        active_game: Optional active game to display.
        last_guess: Optional one-time guess result to display.
        error: Optional setup error message.
        status: HTTP status code.

    Returns:
        A rendered game index response.
    """
    turns = []
    current_turn = None
    current_target_name = ""
    reveal_guess_lat = ""
    reveal_guess_lng = ""
    if active_game is not None:
        turns = list(
            active_game.turns.only(
                "id",
                "target",
                "turn_number",
                "revealed_at",
            ).order_by("turn_number")
        )
        if active_game.status == Game.Status.ACTIVE:
            current_turn = next(
                (turn for turn in turns if turn.revealed_at is None),
                None,
            )
        if current_turn is not None:
            current_target_name = Municipality.objects.only("name").get(
                pk=current_turn.target_id
            ).name
    if last_guess is not None:
        reveal_guess_lat = f"{last_guess.point.y:.6f}"
        reveal_guess_lng = f"{last_guess.point.x:.6f}"
        request.session[MUNICIPALITY_LABEL_ACCESS_SESSION_KEY] = last_guess.turn_id
    else:
        request.session.pop(MUNICIPALITY_LABEL_ACCESS_SESSION_KEY, None)
    return render(
        request,
        "game/index.html",
        {
            "active_game": active_game,
            "current_turn": current_turn,
            "current_target_name": current_target_name,
            "last_guess": last_guess,
            "reveal_guess_lat": reveal_guess_lat,
            "reveal_guess_lng": reveal_guess_lng,
            "show_game_map": active_game is None
            or (current_turn is not None or last_guess is not None),
            "open_auth_choice_modal": (
                active_game is None
                and last_guess is None
                and not request.user.is_authenticated
                and not request.session.session_key
            ),
            "turn_count": TURN_COUNT,
            "turns": turns,
            "error": error,
        },
        status=status,
    )
