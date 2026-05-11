"""Player identity helpers for authenticated and guest games."""

from dataclasses import dataclass

from django.db.models import Q


@dataclass(frozen=True)
class PlayerIdentity:
    """Ownership identity for a game participant.

    A player is either an authenticated user or an anonymous browser session.
    Guest session identities are intentionally separate from account identities
    so statistics and history can stay account-only.
    """

    user: object | None = None
    session_key: str = ""

    @classmethod
    def for_user(cls, user) -> "PlayerIdentity":
        """Build an identity for an authenticated user."""
        return cls(user=user, session_key="")

    @classmethod
    def for_session(cls, session_key: str) -> "PlayerIdentity":
        """Build an identity for a guest browser session."""
        return cls(user=None, session_key=session_key)

    @property
    def is_authenticated(self) -> bool:
        """Return whether this identity belongs to an authenticated user."""
        return bool(getattr(self.user, "is_authenticated", False))

    @property
    def is_guest(self) -> bool:
        """Return whether this identity belongs to a guest browser session."""
        return not self.is_authenticated and bool(self.session_key)

    @property
    def can_own_games(self) -> bool:
        """Return whether this identity is specific enough to own games."""
        return self.is_authenticated or self.is_guest

    def model_fields(self) -> dict[str, object]:
        """Return model fields representing this owner."""
        if self.is_authenticated:
            return {"user": self.user, "session_key": ""}
        if self.is_guest:
            return {"user": None, "session_key": self.session_key}
        return {"user": None, "session_key": ""}

    def owner_query(self, prefix: str = "") -> Q:
        """Return a query condition matching rows owned by this player.

        Args:
            prefix: Optional related-object prefix, for example ``"game"`` when
                filtering turns through their game owner.
        """
        user_lookup = f"{prefix}__user" if prefix else "user"
        session_lookup = f"{prefix}__session_key" if prefix else "session_key"
        if self.is_authenticated:
            return Q(**{user_lookup: self.user, session_lookup: ""})
        if self.is_guest:
            return Q(
                **{
                    f"{user_lookup}__isnull": True,
                    session_lookup: self.session_key,
                }
            )
        return Q(pk__isnull=True)

    def owns(self, obj) -> bool:
        """Return whether a model instance belongs to this player."""
        if self.is_authenticated:
            return obj.user_id == self.user.id and obj.session_key == ""
        if self.is_guest:
            return obj.user_id is None and obj.session_key == self.session_key
        return False


def get_player_identity(request, *, create_session: bool = False) -> PlayerIdentity:
    """Return the player identity represented by the current request.

    Args:
        request: Incoming Django request.
        create_session: Whether to create a session key for anonymous users.

    Returns:
        A user identity for authenticated requests, otherwise a guest session
        identity when a session key exists.
    """
    if request.user.is_authenticated:
        return PlayerIdentity.for_user(request.user)
    if create_session and request.session.session_key is None:
        request.session.save()
    return PlayerIdentity.for_session(request.session.session_key or "")
