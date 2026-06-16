"""ReferralSettings model — the program's singleton configuration.

There is exactly one row. It holds the **current** commission program
(`commission_type` / `commission_value`) that mint snapshots onto each new
``ReferralCoupon``, plus the set of discount coupons that may be cloned as
referral templates (``selected_template_coupon_ids``).
"""
from sqlalchemy.dialects.postgresql import JSONB
from vbwd.extensions import db
from vbwd.models.base import BaseModel

from plugins.referral.referral.models.referral_coupon import ReferralCommissionType


class ReferralSettings(BaseModel):
    """Singleton program settings (one row)."""

    __tablename__ = "referral_settings"

    commission_type = db.Column(
        db.Enum(
            ReferralCommissionType,
            name="referral_commission_type_enum",
            native_enum=True,
            create_constraint=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ReferralCommissionType.ABSOLUTE_TOKENS,
    )
    commission_value = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    selected_template_coupon_ids = db.Column(
        JSONB, nullable=False, default=list
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "commission_type": self.commission_type.value,
            "commission_value": str(self.commission_value),
            "selected_template_coupon_ids": list(
                self.selected_template_coupon_ids or []
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<ReferralSettings(type={self.commission_type.value}, "
            f"value={self.commission_value})>"
        )
