"""Referral plugin models — import to register with SQLAlchemy."""
from plugins.referral.referral.models.referral_coupon import (  # noqa: F401
    ReferralCommissionType,
    ReferralCoupon,
    ReferralCouponStatus,
)
from plugins.referral.referral.models.referral_settings import (  # noqa: F401
    ReferralSettings,
)

__all__ = [
    "ReferralCoupon",
    "ReferralCouponStatus",
    "ReferralCommissionType",
    "ReferralSettings",
]
