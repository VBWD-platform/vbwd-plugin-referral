"""Integration: commission payout on redemption (B2), through the real flow.

Mints a referral coupon, then drives the discount plugin's
``checkout_price_adjustment`` + its ``on_committed`` hook (which writes the
CouponUsage AND publishes ``discount.coupon_redeemed``). The referral plugin's
subscriber pays the issuer a token commission. Asserts both commission modes via
``TokenService.get_balance``, the stamped row, the non-referral no-op,
idempotency on a replayed event, and self-referral rejection.
"""
from decimal import Decimal
from uuid import uuid4

from vbwd.events.bus import event_bus
from vbwd.models.enums import InvoiceStatus, UserRole, UserStatus
from vbwd.models.invoice import UserInvoice
from vbwd.models.user import User

from plugins.discount.discount.checkout_adjustment import (
    checkout_price_adjustment,
)
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
from plugins.referral.referral.models.referral_coupon import (
    ReferralCommissionType,
    ReferralCouponStatus,
)
from plugins.referral.referral.repositories.referral_coupon_repository import (
    ReferralCouponRepository,
)
from plugins.referral.referral.service_factory import build_referral_service


def _seed_user(session, role=UserRole.USER) -> User:
    user = User(
        id=uuid4(),
        email=f"u-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        status=UserStatus.ACTIVE,
        role=role,
    )
    session.add(user)
    session.commit()
    return user


def _seed_template(session, *, value="10.00"):
    discount = DiscountRepository(session).save(
        DiscountRule(
            id=uuid4(),
            name="Referral template",
            slug=f"reftmpl-{uuid4().hex[:6]}",
            discount_type=DiscountType.PERCENTAGE,
            value=Decimal(value),
            scope=DiscountScope.GLOBAL,
            is_active=True,
            priority=10,
        )
    )
    return CouponRepository(session).save(
        Coupon(
            id=uuid4(),
            code=f"TMPL{uuid4().hex[:6].upper()}",
            discount_id=discount.id,
            is_active=True,
        )
    )


def _mint(session, *, issuer, commission_type, commission_value):
    template = _seed_template(session)
    service = build_referral_service(session)
    service.set_settings(
        commission_type=commission_type,
        commission_value=Decimal(commission_value),
        selected_template_coupon_ids=[str(template.id)],
    )
    return service.mint(
        issuer_user_id=issuer.id, issuer_nickname="Bob", raw_prefix="REF_USER_"
    )


def _seed_invoice(session, buyer, amount="100.00") -> UserInvoice:
    invoice = UserInvoice(
        id=uuid4(),
        user_id=buyer.id,
        invoice_number=f"INV-{uuid4().hex[:8]}",
        amount=Decimal(amount),
        currency="EUR",
        status=InvoiceStatus.PAID,
    )
    session.add(invoice)
    session.commit()
    return invoice


def _redeem(session, *, code, buyer, subtotal="100.00"):
    """Drive the discount checkout adjustment + its commit hook (publishes the
    redemption event the referral subscriber reacts to)."""
    result = checkout_price_adjustment(
        code=code,
        subtotal=Decimal(subtotal),
        user_id=str(buyer.id),
        scope="SUBSCRIPTION",
        currency="EUR",
    )
    assert result.valid is True
    invoice = _seed_invoice(session, buyer, amount=subtotal)
    result.on_committed(str(invoice.id), str(buyer.id))
    return invoice.id


def _seed_token_bundle(session):
    """A priced active bundle so percent-of-sale has a token rate (10 tok/unit)."""
    from vbwd.models import TokenBundle

    bundle = TokenBundle(
        id=uuid4(),
        name=f"Bundle {uuid4().hex[:4]}",
        token_amount=1000,
        price=100.0,  # 1000 tokens / 100 currency = 10 tokens per unit
        is_active=True,
        sort_order=0,
    )
    session.add(bundle)
    session.commit()


def test_absolute_tokens_commission_credits_issuer(app, db):
    issuer = _seed_user(db.session)
    buyer = _seed_user(db.session)
    referral_coupon = _mint(
        db.session,
        issuer=issuer,
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value="50",
    )

    _redeem(db.session, code=referral_coupon.coupon_code, buyer=buyer)

    token_service = app.container.token_service()
    assert token_service.get_balance(issuer.id) == 50

    stamped = ReferralCouponRepository(db.session).find_by_code(
        referral_coupon.coupon_code
    )
    assert stamped.status == ReferralCouponStatus.USED
    assert stamped.commission_tokens_paid == 50
    assert stamped.invoice_id is not None
    assert stamped.used_at is not None


def test_percent_of_sale_commission_credits_issuer(app, db):
    _seed_token_bundle(db.session)
    issuer = _seed_user(db.session)
    buyer = _seed_user(db.session)
    referral_coupon = _mint(
        db.session,
        issuer=issuer,
        commission_type=ReferralCommissionType.PERCENT_OF_SALE,
        commission_value="10",  # 10% of net
    )

    # subtotal 200, 10% template discount → net 200; 10% commission = 20 currency
    # at 10 tokens/unit → 200 tokens.
    _redeem(db.session, code=referral_coupon.coupon_code, buyer=buyer, subtotal="200.00")

    token_service = app.container.token_service()
    assert token_service.get_balance(issuer.id) == 200


def test_non_referral_coupon_pays_nothing(app, db):
    buyer = _seed_user(db.session)
    # A plain discount coupon (no referral row).
    discount = DiscountRepository(db.session).save(
        DiscountRule(
            id=uuid4(),
            name="Plain",
            slug=f"plain-{uuid4().hex[:6]}",
            discount_type=DiscountType.PERCENTAGE,
            value=Decimal("10.00"),
            scope=DiscountScope.GLOBAL,
            is_active=True,
            priority=10,
        )
    )
    coupon = CouponRepository(db.session).save(
        Coupon(id=uuid4(), code="PLAIN10", discount_id=discount.id, is_active=True)
    )

    _redeem(db.session, code=coupon.code, buyer=buyer)

    token_service = app.container.token_service()
    assert token_service.get_balance(buyer.id) == 0


def test_duplicate_redemption_event_pays_once(app, db):
    issuer = _seed_user(db.session)
    buyer = _seed_user(db.session)
    referral_coupon = _mint(
        db.session,
        issuer=issuer,
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value="50",
    )

    invoice_id = _redeem(db.session, code=referral_coupon.coupon_code, buyer=buyer)

    # Replay the SAME (coupon, invoice) redemption event directly on the bus.
    event_bus.publish(
        "discount.coupon_redeemed",
        {
            "coupon_id": str(referral_coupon.coupon_id),
            "coupon_code": referral_coupon.coupon_code,
            "user_id": str(buyer.id),
            "invoice_id": str(invoice_id),
            "discount_amount": "10.00",
            "sale_net_amount": "100.00",
        },
    )

    token_service = app.container.token_service()
    assert token_service.get_balance(issuer.id) == 50  # paid once, not twice


def test_self_referral_pays_nothing(app, db):
    issuer = _seed_user(db.session)
    referral_coupon = _mint(
        db.session,
        issuer=issuer,
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value="50",
    )

    # Issuer redeems their own coupon → rejected.
    _redeem(db.session, code=referral_coupon.coupon_code, buyer=issuer)

    token_service = app.container.token_service()
    assert token_service.get_balance(issuer.id) == 0
    stamped = ReferralCouponRepository(db.session).find_by_code(
        referral_coupon.coupon_code
    )
    assert stamped.status == ReferralCouponStatus.ISSUED  # not stamped used
