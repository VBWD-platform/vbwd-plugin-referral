"""Unit tests for ReferralService (mint, settings, commission, stats).

Pure MagicMock repos — no DB. Covers the B0/B2 RED set: settings singleton
get/set, mint code pattern + clone + snapshot, both commission modes,
idempotency, self-referral rejection, non-referral no-op, and stats masking.
"""
import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.referral.referral.models.referral_coupon import (
    ReferralCommissionType,
    ReferralCouponStatus,
)
from plugins.referral.referral.services.referral_service import (
    ReferralError,
    ReferralService,
)

_CODE_PATTERN = re.compile(r"^REF_USER_BOB_[0-9A-F]{16}$")


def _make_settings(commission_type, commission_value, template_ids):
    return SimpleNamespace(
        commission_type=commission_type,
        commission_value=commission_value,
        selected_template_coupon_ids=list(template_ids),
    )


def _build_service(
    *,
    settings_repo=None,
    coupon_repo=None,
    referral_repo=None,
    discount_repo=None,
    token_service=None,
    tokens_per_currency_unit=Decimal("10"),
):
    return ReferralService(
        referral_coupon_repository=referral_repo or MagicMock(),
        referral_settings_repository=settings_repo or MagicMock(),
        coupon_repository=coupon_repo or MagicMock(),
        discount_repository=discount_repo or MagicMock(),
        token_service=token_service or MagicMock(),
        tokens_per_currency_unit_provider=lambda: tokens_per_currency_unit,
    )


def test_get_settings_creates_singleton_when_absent():
    settings_repo = MagicMock()
    settings_repo.get_singleton.return_value = None
    settings_repo.save.side_effect = lambda row: row

    service = _build_service(settings_repo=settings_repo)
    settings = service.get_settings()

    assert settings.commission_type == ReferralCommissionType.ABSOLUTE_TOKENS
    settings_repo.save.assert_called_once()


def test_set_settings_round_trip():
    existing = _make_settings(ReferralCommissionType.ABSOLUTE_TOKENS, Decimal("0"), [])
    settings_repo = MagicMock()
    settings_repo.get_singleton.return_value = existing
    settings_repo.save.side_effect = lambda row: row

    service = _build_service(settings_repo=settings_repo)
    template_id = str(uuid4())
    saved = service.set_settings(
        commission_type=ReferralCommissionType.PERCENT_OF_SALE,
        commission_value=Decimal("15"),
        selected_template_coupon_ids=[template_id],
    )

    assert saved.commission_type == ReferralCommissionType.PERCENT_OF_SALE
    assert saved.commission_value == Decimal("15")
    assert saved.selected_template_coupon_ids == [template_id]


def test_mint_produces_patterned_code_cloned_from_template(monkeypatch):
    template_id = uuid4()
    discount_id = uuid4()
    template_coupon = SimpleNamespace(
        id=template_id,
        discount_id=discount_id,
        max_uses=None,
        max_uses_per_user=1,
        starts_at=None,
        expires_at=None,
    )
    settings = _make_settings(
        ReferralCommissionType.ABSOLUTE_TOKENS, Decimal("50"), [str(template_id)]
    )
    settings_repo = MagicMock()
    settings_repo.get_singleton.return_value = settings

    coupon_repo = MagicMock()
    coupon_repo.find_by_id.return_value = template_coupon
    coupon_repo.find_by_code.return_value = None  # generated code is unique
    saved_coupons = []

    def _save_coupon(coupon):
        coupon.id = uuid4()
        saved_coupons.append(coupon)
        return coupon

    coupon_repo.save.side_effect = _save_coupon

    referral_repo = MagicMock()
    referral_repo.save.side_effect = lambda row: row

    # Deterministic hex so the pattern assertion is exact.
    monkeypatch.setattr(
        "plugins.referral.referral.services.referral_service.generate_hex_token",
        lambda: "F45E2A5DBB6677FF",
    )

    service = _build_service(
        settings_repo=settings_repo,
        coupon_repo=coupon_repo,
        referral_repo=referral_repo,
    )
    issuer_id = uuid4()
    referral_coupon = service.mint(
        issuer_user_id=issuer_id,
        issuer_nickname="Bob",
        raw_prefix="REF_USER_",
    )

    assert _CODE_PATTERN.match(referral_coupon.coupon_code)
    # New discount coupon cloned, sharing the template's discount rule.
    assert len(saved_coupons) == 1
    assert saved_coupons[0].discount_id == discount_id
    # Commission settings snapshotted onto the row at mint.
    assert referral_coupon.commission_type == ReferralCommissionType.ABSOLUTE_TOKENS
    assert referral_coupon.commission_value == Decimal("50")
    assert referral_coupon.issuer_user_id == issuer_id
    assert referral_coupon.template_coupon_id == template_id


def test_mint_without_configured_template_raises():
    settings = _make_settings(ReferralCommissionType.ABSOLUTE_TOKENS, Decimal("10"), [])
    settings_repo = MagicMock()
    settings_repo.get_singleton.return_value = settings

    service = _build_service(settings_repo=settings_repo)
    with pytest.raises(ReferralError):
        service.mint(issuer_user_id=uuid4(), issuer_nickname="Bob", raw_prefix="REF_")


def test_mint_unknown_template_lists_valid(monkeypatch):
    valid_template_id = uuid4()
    valid_coupon = SimpleNamespace(id=valid_template_id, code="SUMMER10")
    settings = _make_settings(
        ReferralCommissionType.ABSOLUTE_TOKENS,
        Decimal("10"),
        [str(valid_template_id)],
    )
    settings_repo = MagicMock()
    settings_repo.get_singleton.return_value = settings

    coupon_repo = MagicMock()
    coupon_repo.find_by_id.return_value = valid_coupon
    coupon_repo.find_by_code.return_value = None  # the requested template is unknown

    service = _build_service(settings_repo=settings_repo, coupon_repo=coupon_repo)
    with pytest.raises(ReferralError) as exc:
        service.mint(
            issuer_user_id=uuid4(),
            issuer_nickname="Bob",
            raw_prefix="REF_",
            template_coupon_code="NOPE",
        )
    assert "SUMMER10" in str(exc.value)


def test_absolute_tokens_commission_credits_fixed_amount():
    coupon_id = uuid4()
    issuer_id = uuid4()
    invoice_id = uuid4()
    referral_coupon = SimpleNamespace(
        id=uuid4(),
        issuer_user_id=issuer_id,
        coupon_code="REF_USER_BOB_F45E2A5DBB6677FF",
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value=Decimal("50"),
        status=ReferralCouponStatus.ISSUED,
        used_at=None,
        discount_amount=None,
        commission_tokens_paid=None,
        invoice_id=None,
    )
    referral_repo = MagicMock()
    referral_repo.find_by_coupon_and_invoice.return_value = None
    referral_repo.find_unused_by_coupon_id.return_value = referral_coupon
    referral_repo.save.side_effect = lambda row: row
    token_service = MagicMock()

    service = _build_service(referral_repo=referral_repo, token_service=token_service)
    result = service.pay_commission_for_redemption(
        coupon_id=coupon_id,
        buyer_user_id=uuid4(),
        invoice_id=invoice_id,
        discount_amount=Decimal("5"),
        sale_net_amount=Decimal("100"),
    )

    token_service.credit_tokens.assert_called_once()
    assert token_service.credit_tokens.call_args.kwargs["amount"] == 50
    assert token_service.credit_tokens.call_args.kwargs["user_id"] == issuer_id
    assert result.status == ReferralCouponStatus.USED
    assert result.commission_tokens_paid == 50
    assert result.invoice_id == invoice_id


def test_percent_of_sale_commission_converts_to_tokens():
    issuer_id = uuid4()
    referral_coupon = SimpleNamespace(
        id=uuid4(),
        issuer_user_id=issuer_id,
        coupon_code="REF_USER_BOB_F45E2A5DBB6677FF",
        commission_type=ReferralCommissionType.PERCENT_OF_SALE,
        commission_value=Decimal("10"),  # 10% of sale
        status=ReferralCouponStatus.ISSUED,
        used_at=None,
        discount_amount=None,
        commission_tokens_paid=None,
        invoice_id=None,
    )
    referral_repo = MagicMock()
    referral_repo.find_by_coupon_and_invoice.return_value = None
    referral_repo.find_unused_by_coupon_id.return_value = referral_coupon
    referral_repo.save.side_effect = lambda row: row
    token_service = MagicMock()

    # 10% of 200 = 20 currency; rate = 10 tokens/unit → 200 tokens.
    service = _build_service(
        referral_repo=referral_repo,
        token_service=token_service,
        tokens_per_currency_unit=Decimal("10"),
    )
    service.pay_commission_for_redemption(
        coupon_id=uuid4(),
        buyer_user_id=uuid4(),
        invoice_id=uuid4(),
        discount_amount=Decimal("20"),
        sale_net_amount=Decimal("200"),
    )

    assert token_service.credit_tokens.call_args.kwargs["amount"] == 200


def test_non_referral_coupon_pays_nothing():
    referral_repo = MagicMock()
    referral_repo.find_by_coupon_and_invoice.return_value = None
    referral_repo.find_unused_by_coupon_id.return_value = None  # not a referral coupon
    token_service = MagicMock()

    service = _build_service(referral_repo=referral_repo, token_service=token_service)
    result = service.pay_commission_for_redemption(
        coupon_id=uuid4(),
        buyer_user_id=uuid4(),
        invoice_id=uuid4(),
        discount_amount=Decimal("5"),
        sale_net_amount=Decimal("100"),
    )

    assert result is None
    token_service.credit_tokens.assert_not_called()


def test_duplicate_redemption_event_pays_once():
    already = SimpleNamespace(status=ReferralCouponStatus.USED)
    referral_repo = MagicMock()
    referral_repo.find_by_coupon_and_invoice.return_value = already  # already processed
    token_service = MagicMock()

    service = _build_service(referral_repo=referral_repo, token_service=token_service)
    result = service.pay_commission_for_redemption(
        coupon_id=uuid4(),
        buyer_user_id=uuid4(),
        invoice_id=uuid4(),
        discount_amount=Decimal("5"),
        sale_net_amount=Decimal("100"),
    )

    assert result is None
    token_service.credit_tokens.assert_not_called()


def test_self_referral_is_rejected():
    issuer_id = uuid4()
    referral_coupon = SimpleNamespace(
        id=uuid4(),
        issuer_user_id=issuer_id,
        coupon_code="REF_USER_BOB_F45E2A5DBB6677FF",
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value=Decimal("50"),
        status=ReferralCouponStatus.ISSUED,
    )
    referral_repo = MagicMock()
    referral_repo.find_by_coupon_and_invoice.return_value = None
    referral_repo.find_unused_by_coupon_id.return_value = referral_coupon
    token_service = MagicMock()

    service = _build_service(referral_repo=referral_repo, token_service=token_service)
    result = service.pay_commission_for_redemption(
        coupon_id=uuid4(),
        buyer_user_id=issuer_id,  # issuer == buyer
        invoice_id=uuid4(),
        discount_amount=Decimal("5"),
        sale_net_amount=Decimal("100"),
    )

    assert result is None
    token_service.credit_tokens.assert_not_called()


def test_stats_query_masks_unused_reveals_used():
    discount_id = uuid4()
    coupon_id_unused = uuid4()
    coupon_id_used = uuid4()
    unused = SimpleNamespace(
        id=uuid4(),
        issuer_user_id=uuid4(),
        issuer_nickname="Bob",
        coupon_code="REF_USER_BOB_F45E2A5DBB6677FF",
        status=ReferralCouponStatus.ISSUED,
        coupon_id=coupon_id_unused,
        discount_amount=None,
        commission_tokens_paid=None,
        issued_at=None,
        used_at=None,
    )
    used = SimpleNamespace(
        id=uuid4(),
        issuer_user_id=uuid4(),
        issuer_nickname="Ann",
        coupon_code="REF_USER_ANN_AABBCCDDEEFF0011",
        status=ReferralCouponStatus.USED,
        coupon_id=coupon_id_used,
        discount_amount=Decimal("5"),
        commission_tokens_paid=50,
        issued_at=None,
        used_at=None,
    )
    referral_repo = MagicMock()
    referral_repo.list_all.return_value = [unused, used]
    coupon_repo = MagicMock()
    coupon_repo.find_by_id.return_value = SimpleNamespace(discount_id=discount_id)
    discount_repo = MagicMock()
    discount_repo.find_by_id.return_value = SimpleNamespace(value=Decimal("10"))

    service = _build_service(
        referral_repo=referral_repo,
        coupon_repo=coupon_repo,
        discount_repo=discount_repo,
    )
    rows = service.stats_query()

    masked = next(r for r in rows if r["status"] == "issued")
    revealed = next(r for r in rows if r["status"] == "used")
    assert masked["coupon_code"] == "REF_USER_BOB_F45ExxxxxxxxxxFF"
    assert revealed["coupon_code"] == "REF_USER_ANN_AABBCCDDEEFF0011"
