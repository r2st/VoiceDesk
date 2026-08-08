"""Pricing plan catalog.

The design document quotes USD list prices. Every monetary value in VoiceDesk
is stored as an integer number of **paise** (1 INR = 100 paise), so the catalog
below is the INR price list: USD prices converted at ~₹83/USD and rounded to
the nearest clean INR price point for the Indian market.

    Starter    $49  -> ₹3,999/mo,  $0.15/min -> ₹12.50/min
    Growth     $99  -> ₹7,999/mo,  $0.10/min -> ₹8.30/min
    Business   $199 -> ₹15,999/mo, $0.05/min -> ₹4.15/min
    Enterprise custom

Annual plans receive a 20% discount (design doc §6.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import PlanTier

#: Goods and Services Tax applied to Indian invoices, in basis points (18%).
GST_BASIS_POINTS = 1800

#: Discount applied to annual pre-payment, in basis points (20%).
ANNUAL_DISCOUNT_BASIS_POINTS = 2000


@dataclass(frozen=True, slots=True)
class Plan:
    tier: PlanTier
    name: str
    monthly_fee_paise: int
    per_minute_paise: int
    included_minutes: int
    max_agents: int | None
    max_languages: int | None
    features: tuple[str, ...]

    @property
    def is_custom(self) -> bool:
        return self.tier is PlanTier.ENTERPRISE

    def annual_fee_paise(self) -> int:
        """12 months less the annual discount, rounded to the nearest paisa."""
        gross = self.monthly_fee_paise * 12
        return gross - (gross * ANNUAL_DISCOUNT_BASIS_POINTS) // 10_000


PLANS: dict[PlanTier, Plan] = {
    PlanTier.STARTER: Plan(
        tier=PlanTier.STARTER,
        name="Starter",
        monthly_fee_paise=399_900,
        per_minute_paise=1_250,
        included_minutes=200,
        max_agents=1,
        max_languages=1,
        features=("basic_analytics", "call_recording"),
    ),
    PlanTier.GROWTH: Plan(
        tier=PlanTier.GROWTH,
        name="Growth",
        monthly_fee_paise=799_900,
        per_minute_paise=830,
        included_minutes=500,
        max_agents=3,
        max_languages=3,
        features=("basic_analytics", "call_recording", "crm_integration", "whatsapp_handoff"),
    ),
    PlanTier.BUSINESS: Plan(
        tier=PlanTier.BUSINESS,
        name="Business",
        monthly_fee_paise=1_599_900,
        per_minute_paise=415,
        included_minutes=2_000,
        max_agents=None,
        max_languages=None,
        features=(
            "advanced_analytics",
            "call_recording",
            "crm_integration",
            "whatsapp_handoff",
            "api_access",
            "live_monitoring",
        ),
    ),
    PlanTier.ENTERPRISE: Plan(
        tier=PlanTier.ENTERPRISE,
        name="Enterprise",
        monthly_fee_paise=0,
        per_minute_paise=0,
        included_minutes=0,
        max_agents=None,
        max_languages=None,
        features=(
            "advanced_analytics",
            "call_recording",
            "crm_integration",
            "whatsapp_handoff",
            "api_access",
            "live_monitoring",
            "dedicated_infrastructure",
            "sla",
            "custom_integrations",
        ),
    ),
}


def get_plan(tier: PlanTier | str) -> Plan:
    """Look up a plan, defaulting unknown tiers to Starter."""
    try:
        key = PlanTier(tier)
    except ValueError:
        key = PlanTier.STARTER
    return PLANS[key]


def paise_to_rupees(paise: int) -> str:
    """Format paise as a rupee string for display: ``399900`` -> ``'3999.00'``."""
    return f"{paise // 100}.{paise % 100:02d}"


def apply_gst(amount_paise: int) -> int:
    """GST on a pre-tax amount, rounded half-up to the nearest paisa."""
    return (amount_paise * GST_BASIS_POINTS + 5_000) // 10_000
