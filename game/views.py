"""Views for game pages."""

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from geo.models import Municipality
from tracking.models import GameEvent
from tracking.services import track_event

from .models import Game, Guess, Turn
from .selectors import get_active_game, get_finished_game_summary
from .services import (
    TURN_COUNT,
    GuessSubmissionError,
    NotEnoughMunicipalitiesError,
    start_game,
    submit_guess,
)


CLIENT_TRACKING_EVENT_TYPES = frozenset(
    {
        GameEvent.Type.MAP_CLICKED,
        GameEvent.Type.PIN_MOVED,
        GameEvent.Type.REVEAL_SHOWN,
        GameEvent.Type.NEXT_TURN_CLICKED,
    }
)
MAX_TRACKING_REQUEST_BYTES = 4096


@login_required
@require_GET
def index(request):
    """Render the current game entry page.

    Args:
        request: The incoming HTTP request.

    Returns:
        A rendered game page for the active game or start form.
    """
    active_game = get_active_game(request.user)
    last_guess = get_last_guess_result(request)
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


@login_required
@require_POST
def start(request):
    """Start or resume an active game for the current user.

    Args:
        request: The incoming HTTP request.

    Returns:
        A redirect to the game index, or a validation response if setup is blocked.
    """
    try:
        start_game(request.user)
    except NotEnoughMunicipalitiesError as error:
        return render_game_index(request, error=str(error), status=400)
    return redirect("game:index")


@login_required
@require_POST
def guess(request):
    """Submit a guess for the current game turn.

    Args:
        request: The incoming HTTP request.

    Returns:
        A redirect to the game index, or a validation response when the guess is
        rejected.
    """
    try:
        result = submit_guess(
            user=request.user,
            turn_id=request.POST.get("turn_id"),
            latitude=request.POST.get("latitude"),
            longitude=request.POST.get("longitude"),
        )
    except GuessSubmissionError as error:
        active_game = get_active_game(request.user)
        return render_game_index(
            request,
            active_game=active_game,
            error=str(error),
            status=400,
        )
    request.session["last_guess_id"] = result.guess.id
    return redirect("game:index")


@login_required
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
        Http404: If the turn does not belong to the current user.
    """
    try:
        event_type, payload = parse_tracking_request(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)

    try:
        turn = Turn.objects.select_related("game").get(
            pk=turn_id,
            game__user=request.user,
        )
    except Turn.DoesNotExist as error:
        raise Http404("Turn not found.") from error

    try:
        validate_tracking_event_state(event_type=event_type, turn=turn)
        track_event(
            user=request.user,
            game=turn.game,
            turn=turn,
            event_type=event_type,
            payload=payload,
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
    if event_type in (GameEvent.Type.MAP_CLICKED, GameEvent.Type.PIN_MOVED):
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
        if turn.revealed_at is None:
            raise ValueError("Tracking event is not valid for this turn state.")
        return

    if event_type == GameEvent.Type.NEXT_TURN_CLICKED:
        next_turn_exists = turn.game.turns.filter(
            revealed_at__isnull=True,
            turn_number__gt=turn.turn_number,
        ).exists()
        if (
            turn.revealed_at is None
            or turn.game.status != Game.Status.ACTIVE
            or not next_turn_exists
        ):
            raise ValueError("Tracking event is not valid for this turn state.")


@login_required
@require_GET
def summary(request, game_id: int):
    """Render the summary for a finished game owned by the current user.

    Args:
        request: The incoming HTTP request.
        game_id: Finished game primary key.

    Returns:
        A rendered summary page for the finished game.

    Raises:
        Http404: If the game is not finished or does not belong to the user.
    """
    game = get_finished_game_summary(request.user, game_id)
    if game is None:
        raise Http404("Game summary not found.")
    return render(request, "game/summary.html", {"game": game})


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
        ValueError: If the declared request body size is invalid or too large.
    """
    content_length = request.META.get("CONTENT_LENGTH")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise ValueError("Tracking content length is invalid.") from None
        if declared_length < 0:
            raise ValueError("Tracking content length is invalid.")
        if declared_length > MAX_TRACKING_REQUEST_BYTES:
            raise ValueError("Tracking payload is too large.")
    return request.body


def get_last_guess_result(request) -> Guess | None:
    """Return the one-time result guess stored in the session.

    Args:
        request: The incoming HTTP request.

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

    return (
        Guess.objects.select_related("turn__game", "turn__target")
        .only(
            "id",
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
            "turn__target__name",
        )
        .filter(pk=guess_pk, user=request.user)
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
            "show_game_map": active_game is not None
            and (current_turn is not None or last_guess is not None),
            "turn_count": TURN_COUNT,
            "turns": turns,
            "error": error,
        },
        status=status,
    )
