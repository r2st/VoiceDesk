"""``/api/v1/auth`` — registration, login, token rotation, team management."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from app.core.deps import CurrentContext, DbSession, RequireAdmin
from app.core.errors import NotFoundError
from app.core.tenancy import tenant_select
from app.models.business import Business, User
from app.schemas.auth import (
    BusinessOut,
    BusinessUpdate,
    LoginRequest,
    PasswordChange,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.schemas.common import MessageResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, request: Request, session: DbSession
) -> RegisterResponse:
    """Create a business and its owner account, returning a signed-in token pair."""
    business, user, tokens = await auth_service.register_business(
        session, payload, user_agent=request.headers.get("user-agent")
    )
    return RegisterResponse(
        business=BusinessOut.model_validate(business),
        user=UserOut.model_validate(user),
        tokens=tokens,
    )


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, request: Request, session: DbSession) -> TokenPair:
    _, tokens = await auth_service.authenticate(
        session, payload, user_agent=request.headers.get("user-agent")
    )
    return tokens


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, request: Request, session: DbSession) -> TokenPair:
    return await auth_service.refresh_tokens(
        session, payload.refresh_token, user_agent=request.headers.get("user-agent")
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(payload: RefreshRequest, session: DbSession) -> MessageResponse:
    await auth_service.revoke_refresh_token(session, payload.refresh_token)
    return MessageResponse(message="Signed out.")


@router.get("/me", response_model=UserOut)
async def me(context: CurrentContext, session: DbSession) -> UserOut:
    user = await session.get(User, context.user_id)
    if user is None:  # pragma: no cover - get_current_context already checked
        raise NotFoundError("User not found.")
    return UserOut.model_validate(user)


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    payload: PasswordChange, context: CurrentContext, session: DbSession
) -> MessageResponse:
    user = await session.get(User, context.user_id)
    if user is None:  # pragma: no cover
        raise NotFoundError("User not found.")
    await auth_service.change_password(
        session, user, payload.current_password, payload.new_password
    )
    return MessageResponse(message="Password changed. Please sign in again.")


# --------------------------------------------------------------------------- #
# Business profile
# --------------------------------------------------------------------------- #
@router.get("/business", response_model=BusinessOut)
async def get_business(context: CurrentContext, session: DbSession) -> BusinessOut:
    business = await session.get(Business, context.business_id)
    if business is None or business.deleted_at is not None:
        raise NotFoundError("Business not found.")
    return BusinessOut.model_validate(business)


@router.patch("/business", response_model=BusinessOut)
async def update_business(
    payload: BusinessUpdate, context: RequireAdmin, session: DbSession
) -> BusinessOut:
    business = await session.get(Business, context.business_id)
    if business is None or business.deleted_at is not None:
        raise NotFoundError("Business not found.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(business, field, value)
    await session.flush()
    return BusinessOut.model_validate(business)


# --------------------------------------------------------------------------- #
# Team
# --------------------------------------------------------------------------- #
@router.get("/users", response_model=list[UserOut])
async def list_users(context: CurrentContext, session: DbSession) -> list[UserOut]:
    result = await session.execute(
        tenant_select(User, context.business_id).order_by(User.created_at)
    )
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate, context: RequireAdmin, session: DbSession
) -> UserOut:
    user = await auth_service.create_user(session, context.business_id, payload)
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, payload: UserUpdate, context: RequireAdmin, session: DbSession
) -> UserOut:
    user = await auth_service.update_user(session, context.business_id, user_id, payload)
    return UserOut.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, context: RequireAdmin, session: DbSession
) -> Response:
    await auth_service.soft_delete_user(session, context.business_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
