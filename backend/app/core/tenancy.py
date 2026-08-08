"""Multi-tenant query helpers.

Every read in the application goes through :func:`tenant_select` so that the
``business_id`` predicate and the soft-delete predicate cannot be forgotten.
PostgreSQL RLS is a backstop, not the primary control (design doc §3.1).
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


def tenant_select(
    model: type[ModelT], business_id: uuid.UUID, *, include_deleted: bool = False
) -> Select:
    """A ``SELECT`` already scoped to one tenant and excluding soft-deleted rows."""
    if not hasattr(model, "business_id"):
        raise TypeError(f"{model.__name__} is not a tenant-scoped model.")
    stmt = select(model).where(model.business_id == business_id)
    if not include_deleted and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    return stmt


async def get_owned_or_404(
    session: AsyncSession,
    model: type[ModelT],
    entity_id: uuid.UUID,
    business_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    label: str | None = None,
) -> ModelT:
    """Fetch one row by id, scoped to the tenant. Raises 404 if absent.

    A row belonging to another tenant is indistinguishable from a missing row,
    so this never leaks the existence of other tenants' data.
    """
    stmt = tenant_select(model, business_id, include_deleted=include_deleted).where(
        model.id == entity_id
    )
    result = await session.execute(stmt)
    entity = result.scalar_one_or_none()
    if entity is None:
        name = label or model.__name__
        raise NotFoundError(f"{name} not found.")
    return entity


async def tenant_exists(
    session: AsyncSession, model: type[ModelT], entity_id: uuid.UUID, business_id: uuid.UUID
) -> bool:
    stmt = tenant_select(model, business_id).where(model.id == entity_id)
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None
