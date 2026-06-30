"""ReferralCoupon model — links an issuer to a cloned discount coupon.

A ``ReferralCoupon`` is minted by an issuer (via the meinchat bot command) and
bound to a new ``discount.Coupon`` cloned from a selected template. The program
commission settings are **snapshotted** onto each row at mint time
(``commission_type`` / ``commission_value``) so later settings changes never
retroactively re-price an already-minted coupon (S92 B.1, auditable).

On redemption the row is stamped (``status=used``, ``used_at``,
``discount_amount``, ``commission_tokens_paid``, ``invoice_id``); the
``(coupon_id, invoice_id)`` uniqueness guards idempotent commission payout.
"""
import enum

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class ReferralCommissionType(str, enum.Enum):
    """How the issuer commission is computed on redemption."""

    PERCENT_OF_SALE = "percent_of_sale"
    ABSOLUTE_TOKENS = "absolute_tokens"


class ReferralCouponStatus(str, enum.Enum):
    """Lifecycle of a referral coupon row."""

    ISSUED = "issued"
    USED = "used"
    EXPIRED = "expired"


class ReferralCoupon(BaseModel):
    """The referral link between an issuer and a discount coupon."""

    __tablename__ = "referral_coupon"
    __table_args__ = (
        UniqueConstraint("coupon_id", "invoice_id", name="uq_referral_coupon_invoice"),
    )

    issuer_user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coupon_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("discount_coupon.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coupon_code = db.Column(db.String(120), nullable=False, index=True)
    issuer_nickname = db.Column(db.String(64), nullable=False)
    template_coupon_id = db.Column(UUID(as_uuid=True), nullable=True)

    commission_type = db.Column(
        db.Enum(
            ReferralCommissionType,
            name="referral_commission_type_enum",
            native_enum=True,
            create_constraint=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    commission_value = db.Column(db.Numeric(12, 4), nullable=False)

    status = db.Column(
        db.Enum(
            ReferralCouponStatus,
            name="referral_coupon_status_enum",
            native_enum=True,
            create_constraint=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ReferralCouponStatus.ISSUED,
        index=True,
    )

    issued_at = db.Column(db.DateTime, nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    discount_amount = db.Column(db.Numeric(12, 2), nullable=True)
    commission_tokens_paid = db.Column(db.Integer, nullable=True)
    invoice_id = db.Column(UUID(as_uuid=True), nullable=True, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "issuer_user_id": str(self.issuer_user_id),
            "coupon_id": str(self.coupon_id),
            "coupon_code": self.coupon_code,
            "issuer_nickname": self.issuer_nickname,
            "template_coupon_id": str(self.template_coupon_id)
            if self.template_coupon_id
            else None,
            "commission_type": self.commission_type.value,
            "commission_value": str(self.commission_value),
            "status": self.status.value,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "discount_amount": str(self.discount_amount)
            if self.discount_amount is not None
            else None,
            "commission_tokens_paid": self.commission_tokens_paid,
            "invoice_id": str(self.invoice_id) if self.invoice_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<ReferralCoupon(code='{self.coupon_code}', "
            f"status={self.status.value})>"
        )
