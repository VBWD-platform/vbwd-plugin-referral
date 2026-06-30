"""ReferralService — mint, settings, commission payout, stats (S92 Track B).

Owns the referral domain: minting a per-user coupon cloned from a selected
discount template (snapshotting commission), reading/writing the singleton
program settings, paying the issuer a token commission on redemption
(idempotent + self-referral-rejecting), and the masked stats query.

Collaborators are injected (DIP): the referral repos, the discount
``CouponRepository`` (the cloned-from + cloned-to coupons live in the discount
plugin), the discount ``DiscountRepository`` (the template's rule for the % the
stats show), the core ``TokenService`` (commission credit), and a token-rate
provider (percent-of-sale → tokens). No other-plugin runtime import is hard:
the discount repos are passed in by the caller (declared dependency).
"""
import logging
from decimal import Decimal
from typing import Callable, List, Optional
from uuid import UUID

from vbwd.models.enums import TokenTransactionType
from vbwd.utils.datetime_utils import utcnow

from plugins.referral.referral.models.referral_coupon import (
    ReferralCommissionType,
    ReferralCoupon,
    ReferralCouponStatus,
)
from plugins.referral.referral.models.referral_settings import ReferralSettings
from plugins.referral.referral.services.coupon_code import (
    build_referral_code,
    generate_hex_token,
    mask_code,
    normalize_prefix,
)

logger = logging.getLogger(__name__)

#: Bounded retries on the (vanishingly rare) generated-code collision.
_MAX_CODE_GENERATION_ATTEMPTS = 5


class ReferralError(Exception):
    """Raised when a referral operation cannot proceed (clear, never silent)."""


class ReferralService:
    """Orchestrates referral coupon minting, settings, and commission payout."""

    def __init__(
        self,
        *,
        referral_coupon_repository,
        referral_settings_repository,
        coupon_repository,
        discount_repository,
        token_service,
        tokens_per_currency_unit_provider: Callable[[], Decimal],
    ) -> None:
        self._referral_coupon_repository = referral_coupon_repository
        self._referral_settings_repository = referral_settings_repository
        self._coupon_repository = coupon_repository
        self._discount_repository = discount_repository
        self._token_service = token_service
        self._tokens_per_currency_unit_provider = tokens_per_currency_unit_provider

    # ── settings ────────────────────────────────────────────────────────────
    def get_settings(self) -> ReferralSettings:
        """Return the singleton settings row, creating defaults if absent."""
        settings = self._referral_settings_repository.get_singleton()
        if settings is None:
            settings = ReferralSettings(
                commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
                commission_value=Decimal("0"),
                selected_template_coupon_ids=[],
            )
            settings = self._referral_settings_repository.save(settings)
        return settings

    def set_settings(
        self,
        *,
        commission_type: ReferralCommissionType,
        commission_value: Decimal,
        selected_template_coupon_ids: List[str],
    ) -> ReferralSettings:
        """Update the singleton settings (creating it on first write)."""
        settings = self.get_settings()
        settings.commission_type = commission_type
        settings.commission_value = Decimal(str(commission_value))
        settings.selected_template_coupon_ids = list(selected_template_coupon_ids)
        return self._referral_settings_repository.save(settings)

    # ── minting ─────────────────────────────────────────────────────────────
    def mint(
        self,
        *,
        issuer_user_id: UUID,
        issuer_nickname: str,
        raw_prefix: str,
        template_coupon_code: Optional[str] = None,
    ) -> ReferralCoupon:
        """Clone a selected discount template into a new per-user referral coupon.

        Resolves the template (explicit ``--template`` code, else the program's
        primary selected template), creates a new ``discount.Coupon`` sharing the
        template's discount rule under a generated ``<PREFIX>_<NICK>_<HEX16>``
        code, snapshots the current commission settings onto a new
        ``ReferralCoupon`` row, and returns it. Raises :class:`ReferralError`
        (never returns a half-built row) on a bad template or exhausted codes.
        """
        settings = self.get_settings()
        template_coupon = self._resolve_template_coupon(settings, template_coupon_code)

        normalized_prefix = normalize_prefix(raw_prefix)
        if not normalized_prefix:
            raise ReferralError("A coupon prefix is required.")

        new_coupon = self._clone_discount_coupon(
            template_coupon=template_coupon,
            normalized_prefix=normalized_prefix,
            issuer_nickname=issuer_nickname,
        )

        referral_coupon = ReferralCoupon(
            issuer_user_id=issuer_user_id,
            coupon_id=new_coupon.id,
            coupon_code=new_coupon.code,
            issuer_nickname=issuer_nickname,
            template_coupon_id=template_coupon.id,
            commission_type=settings.commission_type,
            commission_value=Decimal(str(settings.commission_value)),
            status=ReferralCouponStatus.ISSUED,
            issued_at=utcnow(),
        )
        return self._referral_coupon_repository.save(referral_coupon)

    def available_template_codes(self) -> List[str]:
        """The codes of the discount coupons currently selected as templates."""
        settings = self.get_settings()
        codes: List[str] = []
        for template_id in settings.selected_template_coupon_ids or []:
            coupon = self._coupon_repository.find_by_id(UUID(str(template_id)))
            if coupon is not None:
                codes.append(coupon.code)
        return codes

    def _resolve_template_coupon(
        self, settings: ReferralSettings, template_coupon_code: Optional[str]
    ):
        selected_ids = [
            str(template_id)
            for template_id in (settings.selected_template_coupon_ids or [])
        ]
        if not selected_ids:
            raise ReferralError(
                "No referral coupon template is configured. Ask an admin to "
                "select one on the Promotions → VBWD Referral page."
            )

        if template_coupon_code:
            coupon = self._coupon_repository.find_by_code(template_coupon_code)
            if coupon is None or str(coupon.id) not in selected_ids:
                valid = ", ".join(self.available_template_codes()) or "(none)"
                raise ReferralError(
                    f"Unknown template '{template_coupon_code}'. "
                    f"Valid templates: {valid}."
                )
            return coupon

        # Default to the program's primary (first) selected template.
        primary = self._coupon_repository.find_by_id(UUID(selected_ids[0]))
        if primary is None:
            raise ReferralError(
                "The configured referral template no longer exists. "
                "Ask an admin to re-select one."
            )
        return primary

    def _clone_discount_coupon(
        self, *, template_coupon, normalized_prefix: str, issuer_nickname: str
    ):
        """Create a new discount Coupon sharing the template's discount rule.

        The discount rule (the % / fixed value) is shared — referral coupons are
        distinct codes for the SAME promotion, never deep copies of the rule.
        Retries the generated hex on the rare code collision.
        """
        # Imported here (not at module top) so the referral plugin module loads
        # even when the discount plugin is absent — the discount Coupon class is
        # only needed at mint time, and discount is a declared dependency.
        from plugins.discount.discount.models.coupon import Coupon

        for _attempt in range(_MAX_CODE_GENERATION_ATTEMPTS):
            candidate_code = build_referral_code(
                normalized_prefix, issuer_nickname, generate_hex_token()
            )
            if self._coupon_repository.find_by_code(candidate_code) is not None:
                continue
            new_coupon = Coupon(
                code=candidate_code,
                discount_id=template_coupon.discount_id,
                max_uses=template_coupon.max_uses,
                max_uses_per_user=template_coupon.max_uses_per_user,
                current_uses=0,
                is_active=True,
                starts_at=template_coupon.starts_at,
                expires_at=template_coupon.expires_at,
            )
            return self._coupon_repository.save(new_coupon)

        raise ReferralError(
            "Could not generate a unique coupon code; please try again."
        )

    # ── commission payout (B2) ──────────────────────────────────────────────
    def pay_commission_for_redemption(
        self,
        *,
        coupon_id: UUID,
        buyer_user_id: UUID,
        invoice_id: UUID,
        discount_amount: Decimal,
        sale_net_amount: Decimal,
    ) -> Optional[ReferralCoupon]:
        """Pay the issuer their commission for a redeemed referral coupon.

        Returns the stamped ``ReferralCoupon`` when a commission was paid, or
        ``None`` when the coupon is not a referral coupon, when the redemption
        was already processed (idempotent on ``(coupon_id, invoice_id)``), or
        when the redemption is a rejected self-referral (issuer == buyer).
        """
        # Idempotency: an already-stamped row for this exact (coupon, invoice)
        # means a replayed event — pay nothing more.
        if self._referral_coupon_repository.find_by_coupon_and_invoice(
            coupon_id, invoice_id
        ):
            logger.info(
                "[referral] redemption already processed for coupon %s invoice %s",
                coupon_id,
                invoice_id,
            )
            return None

        referral_coupon = self._referral_coupon_repository.find_unused_by_coupon_id(
            coupon_id
        )
        if referral_coupon is None:
            # Not a referral coupon (a plain discount coupon) → nothing to pay.
            return None

        # Self-referral (D-SelfRef): an issuer redeeming their own code is
        # self-dealing — reject, do not credit, do not stamp.
        if referral_coupon.issuer_user_id == buyer_user_id:
            logger.info(
                "[referral] self-referral rejected for coupon %s (issuer == buyer)",
                coupon_id,
            )
            return None

        commission_tokens = self._compute_commission_tokens(
            referral_coupon=referral_coupon, sale_net_amount=sale_net_amount
        )

        if commission_tokens > 0:
            self._token_service.credit_tokens(
                user_id=referral_coupon.issuer_user_id,
                amount=commission_tokens,
                transaction_type=TokenTransactionType.REFERRAL_COMMISSION,
                reference_id=referral_coupon.id,
                description=(
                    f"Referral commission for coupon {referral_coupon.coupon_code}"
                ),
            )

        referral_coupon.status = ReferralCouponStatus.USED
        referral_coupon.used_at = utcnow()
        referral_coupon.discount_amount = Decimal(str(discount_amount))
        referral_coupon.commission_tokens_paid = commission_tokens
        referral_coupon.invoice_id = invoice_id
        return self._referral_coupon_repository.save(referral_coupon)

    def _compute_commission_tokens(
        self, *, referral_coupon: ReferralCoupon, sale_net_amount: Decimal
    ) -> int:
        """Tokens owed to the issuer per the SNAPSHOTTED commission settings.

        ``absolute_tokens`` → the snapshotted value, a fixed token count.
        ``percent_of_sale`` → ``value%`` of the sale net amount converted to
        tokens at the configured token-bundle rate (D-Commission). Floored to a
        whole token (tokens are integer-credited).
        """
        if referral_coupon.commission_type == ReferralCommissionType.ABSOLUTE_TOKENS:
            return int(Decimal(str(referral_coupon.commission_value)))

        percent = Decimal(str(referral_coupon.commission_value))
        commission_currency = Decimal(str(sale_net_amount)) * percent / Decimal("100")
        tokens_per_currency_unit = self._tokens_per_currency_unit_provider()
        commission_tokens = commission_currency * tokens_per_currency_unit
        return int(commission_tokens)

    # ── stats (masked read) ─────────────────────────────────────────────────
    def stats_query(self) -> List[dict]:
        """All referral coupons as stat rows, masking still-unused codes.

        Per B.1: ``status != used`` → masked code (prefix + nick + first4 + 10×x
        + last2); ``status == used`` → full code revealed (it is spent). The mask
        is applied server-side so a live code never leaves the service.
        """
        rows: List[dict] = []
        for referral_coupon in self._referral_coupon_repository.list_all():
            is_used = referral_coupon.status == ReferralCouponStatus.USED
            discount_rule = self._discount_repository.find_by_id(
                self._coupon_discount_id(referral_coupon.coupon_id)
            )
            rows.append(
                {
                    "id": str(referral_coupon.id),
                    "issuer_user_id": str(referral_coupon.issuer_user_id),
                    "issuer_nickname": referral_coupon.issuer_nickname,
                    "coupon_code": referral_coupon.coupon_code
                    if is_used
                    else mask_code(referral_coupon.coupon_code),
                    "status": referral_coupon.status.value,
                    "discount_value": str(discount_rule.value)
                    if discount_rule
                    else None,
                    "discount_amount": str(referral_coupon.discount_amount)
                    if referral_coupon.discount_amount is not None
                    else None,
                    "commission_tokens_paid": referral_coupon.commission_tokens_paid,
                    "issued_at": referral_coupon.issued_at.isoformat()
                    if referral_coupon.issued_at
                    else None,
                    "used_at": referral_coupon.used_at.isoformat()
                    if referral_coupon.used_at
                    else None,
                }
            )
        return rows

    def _coupon_discount_id(self, coupon_id: UUID):
        coupon = self._coupon_repository.find_by_id(coupon_id)
        return coupon.discount_id if coupon else None

    def delete_coupons(self, referral_coupon_ids: List[str]) -> int:
        """Delete referral stat rows by id; return how many were removed."""
        parsed_ids = [UUID(str(value)) for value in referral_coupon_ids]
        return self._referral_coupon_repository.delete_by_ids(parsed_ids)
