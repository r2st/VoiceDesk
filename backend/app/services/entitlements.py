"""Whether a tenant is currently entitled to place and receive calls.

Separate from the plan limits in :mod:`app.services.plans`, which cap what a
tenant may configure (agents, languages). This module answers the narrower
question of whether the account itself is in good standing — a trial that has
lapsed, or a subscription that was cancelled.

Deliberately scoped to the call path. A suspended tenant keeps full read
access to the dashboard, its analytics and its invoices, because the whole
point of suspending rather than deleting is that the owner can see what they
owe and pay it. Locking them out of the billing screen would make the
suspension unresolvable.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, QuotaExceededError
from app.core.logging import get_logger
from app.models.business import Business
from app.models.enums import BusinessStatus

logger = get_logger(__name__)

#: Statuses that stop a tenant from originating or answering calls. ``trial``
#: is absent on purpose: a running trial is a paying-customer-to-be and must
#: be able to make the calls that sell the product.
CALL_BLOCKING_STATUSES: dict[str, str] = {
    BusinessStatus.SUSPENDED.value: (
        "This account is suspended. Settle the outstanding balance or upgrade "
        "the plan to resume calling."
    ),
    BusinessStatus.CANCELLED.value: "This account has been cancelled.",
}


async def require_calling_entitlement(session: AsyncSession, business_id: uuid.UUID) -> Business:
    """Return the tenant, or raise if it may not transact.

    Raises ``QuotaExceededError`` (HTTP 402) rather than a 403: the condition
    is nearly always billing-remediable, and a payment-required response is
    what tells a client to route the user to the upgrade screen.
    """
    business = await session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise NotFoundError("Business not found.")

    reason = CALL_BLOCKING_STATUSES.get(business.status)
    if reason is not None:
        logger.info("Blocked call for %s business %s", business.status, business_id)
        raise QuotaExceededError(
            reason,
            details={"status": business.status, "business_id": str(business_id)},
        )
    return business
