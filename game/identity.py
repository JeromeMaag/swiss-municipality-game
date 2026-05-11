"""Player identity helpers for authenticated and guest games."""

from dataclasses import dataclass
from uuid import uuid4

from django.db.models import Q


GUEST_PLAYER_SESSION_KEY = "guest_player_key"


@dataclass(frozen=True)
class PlayerIdentity:
    """Ownership identity for a game participant.

    A player is either an authenticated user or an anonymous guest key stored in
    the browser session. Guest identities are intentionally separate from account
    identities so statistics and history can stay account-only.
    """

    user: object | None = None
    guest_key: str = ""

    @classmethod
    def for_user(cls, user) -> "PlayerIdentity":
        """Build an identity for an authenticated user."""
        return cls(user=user, guest_key="")

    @classmethod
    def for_guest(cls, guest_key: str) -> "PlayerIdentity":
        """Build an identity for a guest browser session."""
        return cls(user=None, guest_key=guest_key)

    @property
    def is_authenticated(self) -> bool:
        """Return whether this identity belongs to an authenticated user."""
        return bool(getattr(self.user, "is_authenticated", False))

    @property
    def is_guest(self) -> bool:
        """Return whether this identity belongs to a guest browser session."""
        return not self.is_authenticated and bool(self.guest_key)

    @property
    def can_own_games(self) -> bool:
        """Return whether this identity is specific enough to own games."""
        return self.is_authenticated or self.is_guest

    def model_fields(self) -> dict[str, object]:
        """Return model fields representing this owner."""
        if self.is_authenticated:
            return {"user": self.user, "guest_key": ""}
        if self.is_guest:
            return {"user": None, "guest_key": self.guest_key}
        return {"user": None, "guest_key": ""}

    def owner_query(self, prefix: str = "") -> Q:
        """Return a query condition matching rows owned by this player.

        Args:
            prefix: Optional related-object prefix, for example ``"game"`` when
                filtering turns through their game owner.
        """
        user_lookup = f"{prefix}__user" if prefix else "user"
        guest_lookup = f"{prefix}__guest_key" if prefix else "guest_key"
        if self.is_authenticated:
            return Q(**{user_lookup: self.user, guest_lookup: ""})
        if self.is_guest:
            return Q(
                **{
                    f"{user_lookup}__isnull": True,
                    guest_lookup: self.guest_key,
                }
            )
        return Q(pk__isnull=True)

    def owns(self, obj) -> bool:
        """Return whether a model instance belongs to this player."""
        if self.is_authenticated:
            return obj.user_id == self.user.id and obj.guest_key == ""
        if self.is_guest:
            return obj.user_id is None and obj.guest_key == self.guest_key
        return False


def get_player_identity(request, *, create_session: bool = False) -> PlayerIdentity:
    """Return the player identity represented by the current request.

    Args:
        request: Incoming Django request.
        create_session: Whether to create a guest key for anonymous users.

    Returns:
        A user identity for authenticated requests, otherwise a guest identity
        when the browser session already has a guest key.
    """
    if request.user.is_authenticated:
        return PlayerIdentity.for_user(request.user)
    guest_key = request.session.get(GUEST_PLAYER_SESSION_KEY, "")
    if create_session and not guest_key:
        guest_key = uuid4().hex
        request.session[GUEST_PLAYER_SESSION_KEY] = guest_key
    return PlayerIdentity.for_guest(guest_key or "")
