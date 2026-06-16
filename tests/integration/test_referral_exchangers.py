"""Integration: referral entity exchanger (real PG) — S92 B4.

* ``referral_coupons`` round-trips by ``coupon_code`` over JSON **and** CSV.
* **Masking preserved through export (D-ExportMask):** a still-unused row
  exports the MASKED code (never the live hex); a ``used`` row exports the full
  code. The exporter and ``ReferralService.stats_query`` share ONE mask
  implementation (``coupon_code.mask_code``) so a CSV/JSON export can never leak
  a live unused code.
* registration: after ``ReferralPlugin._register_data_exchangers`` the exchanger
  appears in ``data_exchange_registry`` under cluster ``sales`` with the
  ``referral.view`` / ``referral.manage`` permissions and JSON+CSV formats.

Data is seeded through the ORM session (no raw SQL); the shared ``db`` fixture
isolates each test in a rolled-back transaction.

Engineering requirements (binding, restated): TDD-first; DevOps-first; SOLID
(one exchanger per entity, narrow ports); DI (session injected); DRY (one mask
fn); Liskov; clean code; no overengineering. Quality guard:
``bin/pre-commit-check.sh --plugin referral --full``.
"""
from decimal import Decimal
from uuid import uuid4

from vbwd.models.enums import UserRole, UserStatus
from vbwd.models.user import User
from vbwd.services.data_exchange.envelope import build_envelope, rows_to_csv
from vbwd.services.data_exchange.port import CLUSTER_SALES, ExportSelector

from plugins.discount.discount.models.coupon import Coupon
from plugins.discount.discount.models.discount import (
    DiscountRule,
    DiscountScope,
    DiscountType,
)
from plugins.referral.referral.models.referral_coupon import (
    ReferralCommissionType,
    ReferralCoupon,
    ReferralCouponStatus,
)
from plugins.referral.referral.services.coupon_code import mask_code
from plugins.referral.referral.services.data_exchange.referral_exchangers import (
    build_referral_exchangers,
)

#: A fixed 16-hex token so the masked form is exactly assertable.
_HEX_TOKEN = "F45E2A5DBB6677FF"
_UNUSED_CODE = f"REF_USER_BOB_{_HEX_TOKEN}"
_MASKED_UNUSED_CODE = "REF_USER_BOB_F45ExxxxxxxxxxFF"
_USED_CODE = f"REF_USER_ANN_{_HEX_TOKEN}"


def _exchanger(session):
    return {
        exchanger.entity_key: exchanger
        for exchanger in build_referral_exchangers(session)
    }["referral_coupons"]


def _seed_user(session) -> User:
    user = User(
        id=uuid4(),
        email=f"issuer-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        status=UserStatus.ACTIVE,
        role=UserRole.USER,
    )
    session.add(user)
    session.flush()
    return user


def _seed_discount_coupon(session) -> Coupon:
    discount = DiscountRule(
        id=uuid4(),
        name="Ref D",
        slug=f"ref-d-{uuid4().hex[:6]}",
        discount_type=DiscountType.PERCENTAGE,
        value=Decimal("10.00"),
        scope=DiscountScope.GLOBAL,
        is_active=True,
        priority=10,
    )
    session.add(discount)
    session.flush()
    coupon = Coupon(
        id=uuid4(),
        code=f"DISC-{uuid4().hex[:6]}",
        discount_id=discount.id,
        is_active=True,
    )
    session.add(coupon)
    session.flush()
    return coupon


def _seed_referral_coupon(
    session, *, code: str, status: ReferralCouponStatus
) -> ReferralCoupon:
    issuer = _seed_user(session)
    coupon = _seed_discount_coupon(session)
    referral_coupon = ReferralCoupon(
        id=uuid4(),
        issuer_user_id=issuer.id,
        coupon_id=coupon.id,
        coupon_code=code,
        issuer_nickname="Bob",
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value=Decimal("50"),
        status=status,
    )
    session.add(referral_coupon)
    session.commit()
    return referral_coupon


class TestReferralCouponsRoundTrip:
    def test_round_trip_by_code_json(self, db):
        # A used coupon round-trips its full code (masking does not apply).
        _seed_referral_coupon(
            db.session, code=_USED_CODE, status=ReferralCouponStatus.USED
        )
        exchanger = _exchanger(db.session)

        before = exchanger.export(
            ExportSelector(ids=[_USED_CODE]), include_pii=False
        ).rows
        assert before and before[0]["coupon_code"] == _USED_CODE

        db.session.query(ReferralCoupon).filter(
            ReferralCoupon.coupon_code == _USED_CODE
        ).delete()
        db.session.commit()

        payload = build_envelope("referral_coupons", before, instance="test")
        result = exchanger.import_(payload, mode="upsert", dry_run=False)
        assert result.created == 1

        rebuilt = (
            db.session.query(ReferralCoupon)
            .filter(ReferralCoupon.coupon_code == _USED_CODE)
            .first()
        )
        assert rebuilt is not None
        assert rebuilt.issuer_nickname == "Bob"

    def test_csv_export_round_trip(self, db):
        _seed_referral_coupon(
            db.session, code=_USED_CODE, status=ReferralCouponStatus.USED
        )
        exchanger = _exchanger(db.session)
        assert "csv" in exchanger.supported_formats

        rows = exchanger.export(
            ExportSelector(ids=[_USED_CODE]), include_pii=False
        ).rows
        csv_text = rows_to_csv(rows)
        header_line = csv_text.splitlines()[0]
        assert "coupon_code" in header_line
        assert _USED_CODE in csv_text


class TestMaskingPreservedThroughExport:
    def test_unused_coupon_export_is_masked(self, db):
        _seed_referral_coupon(
            db.session, code=_UNUSED_CODE, status=ReferralCouponStatus.ISSUED
        )
        exchanger = _exchanger(db.session)

        rows = exchanger.export(
            ExportSelector(ids=[_UNUSED_CODE]), include_pii=False
        ).rows
        assert len(rows) == 1
        exported_code = rows[0]["coupon_code"]
        # The exact masked format — never the live hex middle.
        assert exported_code == _MASKED_UNUSED_CODE
        assert exported_code == mask_code(_UNUSED_CODE)
        assert _HEX_TOKEN not in exported_code

    def test_unused_coupon_csv_export_is_masked(self, db):
        _seed_referral_coupon(
            db.session, code=_UNUSED_CODE, status=ReferralCouponStatus.ISSUED
        )
        exchanger = _exchanger(db.session)

        rows = exchanger.export(
            ExportSelector(ids=[_UNUSED_CODE]), include_pii=False
        ).rows
        csv_text = rows_to_csv(rows)
        assert _MASKED_UNUSED_CODE in csv_text
        assert _UNUSED_CODE not in csv_text

    def test_used_coupon_export_is_full(self, db):
        _seed_referral_coupon(
            db.session, code=_USED_CODE, status=ReferralCouponStatus.USED
        )
        exchanger = _exchanger(db.session)

        rows = exchanger.export(
            ExportSelector(ids=[_USED_CODE]), include_pii=False
        ).rows
        assert len(rows) == 1
        assert rows[0]["coupon_code"] == _USED_CODE


class TestRegistration:
    def test_on_enable_registers_referral_exchanger(self, db):
        from vbwd.services.data_exchange.registry import data_exchange_registry
        from plugins.referral import ReferralPlugin

        plugin = ReferralPlugin()
        plugin.initialize({})
        plugin._register_data_exchangers()

        by_key = {
            exchanger.entity_key: exchanger
            for exchanger in data_exchange_registry.all()
        }
        assert "referral_coupons" in by_key
        exchanger = by_key["referral_coupons"]
        assert exchanger.cluster == CLUSTER_SALES
        assert exchanger.natural_key == "coupon_code"
        assert exchanger.export_permission == "referral.view"
        assert exchanger.import_permission == "referral.manage"
        assert exchanger.supported_formats == frozenset({"json", "csv"})
