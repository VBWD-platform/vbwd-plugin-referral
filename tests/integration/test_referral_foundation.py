"""Integration: referral plugin foundation (B0) + permission catalog.

Drives the real ``ReferralService`` (built via the service factory) against a
PostgreSQL DB: settings singleton round-trip, mint produces a patterned unique
code cloned from a selected discount template + creates the row, and the
``meinchat_can:generate_coupons`` permission appears in the catalog.
"""
import re
from decimal import Decimal
from uuid import uuid4

from vbwd.models.enums import UserRole, UserStatus
from vbwd.models.user import User
from vbwd.services.permission_catalog import collect_permission_catalog

from plugins.discount.discount.models.coupon import Coupon
from plugins.discount.discount.models.discount import (
    DiscountRule,
    DiscountScope,
    DiscountType,
)
from plugins.discount.discount.repositories.coupon_repository import CouponRepository
from plugins.discount.discount.repositories.discount_repository import (
    DiscountRepository,
)
from plugins.referral.referral.models.referral_coupon import ReferralCommissionType
from plugins.referral.referral.service_factory import build_referral_service

_CODE_PATTERN = re.compile(r"^REF_USER_BOB_[0-9A-F]{16}$")


def _seed_template_coupon(session, code="SUMMER10"):
    discount = DiscountRepository(session).save(
        DiscountRule(
            id=uuid4(),
            name=f"D {code}",
            slug=f"d-{code.lower()}-{uuid4().hex[:6]}",
            discount_type=DiscountType.PERCENTAGE,
            value=Decimal("10.00"),
            scope=DiscountScope.GLOBAL,
            is_active=True,
            priority=10,
        )
    )
    coupon = CouponRepository(session).save(
        Coupon(id=uuid4(), code=code, discount_id=discount.id, is_active=True)
    )
    return discount, coupon


def _seed_user(session) -> User:
    user = User(
        id=uuid4(),
        email=f"issuer-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        status=UserStatus.ACTIVE,
        role=UserRole.USER,
    )
    session.add(user)
    session.commit()
    return user


def test_settings_singleton_round_trip(db):
    service = build_referral_service(db.session)
    _, template = _seed_template_coupon(db.session)

    service.set_settings(
        commission_type=ReferralCommissionType.PERCENT_OF_SALE,
        commission_value=Decimal("15"),
        selected_template_coupon_ids=[str(template.id)],
    )

    reloaded = build_referral_service(db.session).get_settings()
    assert reloaded.commission_type == ReferralCommissionType.PERCENT_OF_SALE
    assert reloaded.commission_value == Decimal("15.0000")
    assert reloaded.selected_template_coupon_ids == [str(template.id)]


def test_mint_creates_patterned_coupon_and_row(db):
    user = _seed_user(db.session)
    _, template = _seed_template_coupon(db.session)
    service = build_referral_service(db.session)
    service.set_settings(
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value=Decimal("50"),
        selected_template_coupon_ids=[str(template.id)],
    )

    referral_coupon = service.mint(
        issuer_user_id=user.id,
        issuer_nickname="Bob",
        raw_prefix="REF_USER_",
    )

    assert _CODE_PATTERN.match(referral_coupon.coupon_code)
    # The cloned discount coupon exists and shares the template's discount rule.
    cloned = CouponRepository(db.session).find_by_code(referral_coupon.coupon_code)
    assert cloned is not None
    assert cloned.discount_id == template.discount_id
    # Snapshot present on the row.
    assert referral_coupon.commission_type == ReferralCommissionType.ABSOLUTE_TOKENS
    assert referral_coupon.commission_value == Decimal("50.0000")


def test_generate_coupons_permission_in_catalog(app):
    with app.app_context():
        catalog = collect_permission_catalog(
            plugin_manager=getattr(app, "plugin_manager", None)
        )
    referral_permissions = catalog.get("referral", [])
    keys = {entry["key"] for entry in referral_permissions}
    assert "meinchat_can:generate_coupons" in keys
    assert "referral.view" in keys
    assert "referral.manage" in keys
