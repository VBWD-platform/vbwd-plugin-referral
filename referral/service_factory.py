"""Build a fully-wired ``ReferralService`` from the running app (DRY single home).

Both the bot command handler and the redemption event handler need the same
collaborator graph; this is the one place that assembles it so the wiring lives
in exactly one place (DI through ``current_app.container`` + the live session).
"""
from decimal import Decimal

from vbwd.extensions import db

from plugins.referral.referral.repositories.referral_coupon_repository import (
    ReferralCouponRepository,
)
from plugins.referral.referral.repositories.referral_settings_repository import (
    ReferralSettingsRepository,
)
from plugins.referral.referral.services.referral_service import ReferralService
from plugins.referral.referral.services.token_rate import tokens_per_currency_unit


def build_referral_service(session=None) -> ReferralService:
    """Assemble a ``ReferralService`` bound to ``session`` (default: db.session)."""
    from flask import current_app

    from plugins.discount.discount.repositories.coupon_repository import (
        CouponRepository,
    )
    from plugins.discount.discount.repositories.discount_repository import (
        DiscountRepository,
    )
    from vbwd.repositories.token_bundle_repository import TokenBundleRepository

    active_session = session if session is not None else db.session
    token_service = current_app.container.token_service()
    token_bundle_repository = TokenBundleRepository(active_session)

    return ReferralService(
        referral_coupon_repository=ReferralCouponRepository(active_session),
        referral_settings_repository=ReferralSettingsRepository(active_session),
        coupon_repository=CouponRepository(active_session),
        discount_repository=DiscountRepository(active_session),
        token_service=token_service,
        tokens_per_currency_unit_provider=lambda: _rate_or_zero(
            token_bundle_repository
        ),
    )


def _rate_or_zero(token_bundle_repository) -> Decimal:
    return tokens_per_currency_unit(token_bundle_repository)
