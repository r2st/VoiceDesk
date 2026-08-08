"""FastAPI dependencies: authentication, tenant context and role gating."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, PermissionError_
from app.core.security import decode_token
from app.db.session import get_db
from app.models.business import User
from app.models.enums import UserRole

_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The authenticated principal. ``business_id`` scopes every query."""

    user_id: uuid.UUID
    business_id: uuid.UUID
    role: UserRole
    email: str

    def require_role(self, *allowed: UserRole) -> None:
        if self.role not in allowed:
            raise PermissionError_(f"Role '{self.role}' is not permitted to perform this action.")


async def get_current_context(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> TenantContext:
    """Resolve the bearer token into a verified, still-active tenant principal."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")

    payload = decode_token(credentials.credentials, expected_type="access")
    try:
        user_id = uuid.UUID(payload["sub"])
        business_id = uuid.UUID(payload["business_id"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token claims.") from exc

    # The token is signed, but the user may have been deactivated or soft
    # deleted since it was issued — always re-check against the database.
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None or not user.is_active:
        raise AuthenticationError("User is no longer active.")
    if user.business_id != business_id:
        raise AuthenticationError("Token does not match the user's business.")

    context = TenantContext(
        user_id=user.id,
        business_id=user.business_id,
        role=UserRole(user.role),
        email=user.email,
    )
    request.state.tenant = context
    return context


CurrentContext = Annotated[TenantContext, Depends(get_current_context)]


def require_roles(*allowed: UserRole):
    """Dependency factory gating an endpoint to a set of roles."""

    async def _dependency(context: CurrentContext) -> TenantContext:
        context.require_role(*allowed)
        return context

    return _dependency


#: Roles permitted to mutate configuration (agents, numbers, settings).
RequireAdmin = Annotated[TenantContext, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
#: Roles permitted to operate calls (includes supervisors).
RequireOperator = Annotated[
    TenantContext,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.SUPERVISOR)),
]
