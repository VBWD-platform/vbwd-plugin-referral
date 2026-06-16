"""Data access for referral_coupon rows."""
from typing import List, Optional
from uuid import UUID

from plugins.referral.referral.models.referral_coupon import ReferralCoupon


class ReferralCouponRepository:
    """Thin wrapper over the SQLAlchemy session for ReferralCoupon."""

    def __init__(self, session) -> None:
        self._session = session

    def find_by_id(self, referral_coupon_id: UUID) -> Optional[ReferralCoupon]:
        return (
            self._session.query(ReferralCoupon)
            .filter(ReferralCoupon.id == referral_coupon_id)
            .one_or_none()
        )

    def find_by_coupon_id(self, coupon_id: UUID) -> Optional[ReferralCoupon]:
        """Find the (still-unredeemed) referral row for a discount coupon.

        A referral coupon is single-use per code; the unredeemed row is the
        one with ``invoice_id IS NULL``. Once stamped on redemption the lookup
        for the mapping uses :meth:`find_unused_by_coupon_id`.
        """
        return (
            self._session.query(ReferralCoupon)
            .filter(ReferralCoupon.coupon_id == coupon_id)
            .first()
        )

    def find_unused_by_coupon_id(
        self, coupon_id: UUID
    ) -> Optional[ReferralCoupon]:
        """The not-yet-redeemed referral row for a discount coupon, if any."""
        return (
            self._session.query(ReferralCoupon)
            .filter(ReferralCoupon.coupon_id == coupon_id)
            .filter(ReferralCoupon.invoice_id.is_(None))
            .first()
        )

    def find_by_coupon_and_invoice(
        self, coupon_id: UUID, invoice_id: UUID
    ) -> Optional[ReferralCoupon]:
        """The redemption-stamped row for an exact ``(coupon, invoice)`` pair.

        The single source of idempotency: a replayed redemption event finds
        the already-stamped row here and pays nothing more.
        """
        return (
            self._session.query(ReferralCoupon)
            .filter(ReferralCoupon.coupon_id == coupon_id)
            .filter(ReferralCoupon.invoice_id == invoice_id)
            .first()
        )

    def find_by_code(self, coupon_code: str) -> Optional[ReferralCoupon]:
        return (
            self._session.query(ReferralCoupon)
            .filter(ReferralCoupon.coupon_code == coupon_code)
            .first()
        )

    def list_all(self) -> List[ReferralCoupon]:
        return (
            self._session.query(ReferralCoupon)
            .order_by(ReferralCoupon.issued_at.desc())
            .all()
        )

    def save(self, referral_coupon: ReferralCoupon) -> ReferralCoupon:
        self._session.add(referral_coupon)
        self._session.flush()
        return referral_coupon

    def delete_by_ids(self, referral_coupon_ids: List[UUID]) -> int:
        """Delete referral rows by id; return the number actually removed."""
        if not referral_coupon_ids:
            return 0
        deleted = (
            self._session.query(ReferralCoupon)
            .filter(ReferralCoupon.id.in_(referral_coupon_ids))
            .delete(synchronize_session=False)
        )
        self._session.flush()
        return deleted
