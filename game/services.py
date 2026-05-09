"""Services for game lifecycle operations."""

import random

from django.db import IntegrityError, transaction

from geo.selectors import get_current_municipalities
from tracking.models import GameEvent
from tracking.services import track_event

from .models import Game, Turn
from .selectors import get_active_game


TURN_COUNT = 5


class NotEnoughMunicipalitiesError(ValueError):
    """Raised when there are not enough active municipalities to start a game."""


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
