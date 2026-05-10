"""Views for game pages."""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from geo.models import Municipality

from .models import Game, Guess
from .selectors import get_active_game, get_finished_game_summary
from .services import (
    TURN_COUNT,
    GuessSubmissionError,
    NotEnoughMunicipalitiesError,
    start_game,
    submit_guess,
)


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
