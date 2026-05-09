"""Views for game pages."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .selectors import get_active_game, get_current_turn
from .services import TURN_COUNT, NotEnoughMunicipalitiesError, start_game


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
    return render_game_index(request, active_game=active_game)


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


def render_game_index(
    request,
    active_game=None,
    error: str = "",
    status: int = 200,
):
    """Render the game index template.

    Args:
        request: The incoming HTTP request.
        active_game: Optional active game to display.
        error: Optional setup error message.
        status: HTTP status code.

    Returns:
        A rendered game index response.
    """
    turns = []
    if active_game is not None:
        turns = list(
            active_game.turns.select_related("target__canton").order_by("turn_number")
        )
    return render(
        request,
        "game/index.html",
        {
            "active_game": active_game,
            "current_turn": get_current_turn(active_game),
            "turn_count": TURN_COUNT,
            "turns": turns,
            "error": error,
        },
        status=status,
    )
