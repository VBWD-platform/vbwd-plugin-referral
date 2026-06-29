"""Referral plugin — internal VBWD referral coupons (S92 Track B).

A meinchat bot command mints per-user referral coupons cloned from a selected
discount template; redemption pays the **issuer** a token commission. The plugin
is also a ``bot-base`` consumer (it structurally implements
``BotCommandProvider``) so its command lights up over every bot adapter
(meinchat now, Telegram too) with no consumer change.

Dependencies (declared): ``discount`` (the coupon + redemption record this
clones / listens to) and ``meinchat`` (the issuer nickname). bot-base is a SOFT
bridge — its neutral DTOs are imported lazily inside the bot methods so this
module loads even when bot-base is disabled (chat/cms-ai precedent).
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from flask import current_app

from vbwd.plugins.base import BasePlugin, PluginMetadata

if TYPE_CHECKING:  # pragma: no cover
    from flask import Blueprint

    from plugins.bot_base.bot_base.types import BotCommand, BotInbound, BotReply


DEFAULT_CONFIG = {
    "debug_mode": False,
}

BOT_NAMESPACE = "referral"
NEW_COUPON_COMMAND = "referral_program_new_coupon"
GENERATE_PERMISSION = "meinchat_can:generate_coupons"


class ReferralPlugin(BasePlugin):
    """Internal referral coupons + issuer commission.

    Class MUST be defined in __init__.py (manager discovery checks
    ``obj.__module__``).
    """

    #: bot-base routes this namespace's commands / actions here (D1/D7).
    bot_namespace = BOT_NAMESPACE

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="referral",
            version="26.6",
            description="Internal VBWD referral coupons with issuer token commission",
            author="VBWD",
            dependencies=["discount", "meinchat"],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    @property
    def admin_permissions(self):
        return [
            {
                "key": GENERATE_PERMISSION,
                "label": "Generate referral coupons",
                "group": "Meinchat",
            },
            {
                "key": "referral.view",
                "label": "View referral statistics",
                "group": "Referral",
            },
            {
                "key": "referral.manage",
                "label": "Manage referral program",
                "group": "Referral",
            },
        ]

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.referral.referral.routes import referral_bp

        return referral_bp

    def get_url_prefix(self) -> Optional[str]:
        # Absolute /api/v1/admin/referral/* paths defined on the blueprint.
        return ""

    def on_enable(self) -> None:
        # Import models so SQLAlchemy registers the referral tables.
        import plugins.referral.referral.models  # noqa: F401

        from vbwd.plugins.di_helpers import register_repositories
        from plugins.referral.referral.repositories.referral_coupon_repository import (
            ReferralCouponRepository,
        )
        from plugins.referral.referral.repositories.referral_settings_repository import (  # noqa: E501
            ReferralSettingsRepository,
        )

        container = getattr(current_app, "container", None)
        if container is not None:
            register_repositories(
                container,
                {
                    "referral_coupon_repository": ReferralCouponRepository,
                    "referral_settings_repository": ReferralSettingsRepository,
                },
            )

        self._register_data_exchangers()

    def _register_data_exchangers(self) -> None:
        """Register the referral entity exchanger into the data-exchange seam.

        Core declares none of these (it stays agnostic); the plugin adds the
        ``referral_coupons`` exchanger on enable through the shared ``db.session``
        so referral statistics appear on the generic Settings → Import/Export
        page (cluster ``sales``). Export masks still-unused codes (D-ExportMask).
        Clear-safe: re-registering replaces by key (per-test app re-enable).
        """
        import logging

        try:
            from vbwd.extensions import db
            from plugins.referral.referral.services.data_exchange.referral_exchangers import (  # noqa: E501
                register_referral_exchangers,
            )

            register_referral_exchangers(db.session)
        except Exception as exchanger_error:
            logging.getLogger(__name__).warning(
                "[referral] Failed to register data exchangers: %s", exchanger_error
            )

    def on_disable(self) -> None:
        pass

    # ── redemption commission (B2) ───────────────────────────────────────────
    def register_event_handlers(self, event_bus) -> None:
        """Subscribe to the discount redemption signal to pay commission."""
        event_bus.subscribe("discount.coupon_redeemed", self._on_coupon_redeemed)

    def _on_coupon_redeemed(self, event_name: str, data: dict) -> None:
        """Pay the issuer a commission when a referral coupon is redeemed.

        Idempotent + self-referral-rejecting (enforced in the service). A plain
        (non-referral) discount coupon redemption is a no-op here.
        """
        from uuid import UUID

        from plugins.referral.referral.service_factory import build_referral_service

        coupon_id = data.get("coupon_id")
        invoice_id = data.get("invoice_id")
        buyer_user_id = data.get("user_id")
        if not coupon_id or not invoice_id or not buyer_user_id:
            return

        service = build_referral_service()
        service.pay_commission_for_redemption(
            coupon_id=UUID(str(coupon_id)),
            buyer_user_id=UUID(str(buyer_user_id)),
            invoice_id=UUID(str(invoice_id)),
            discount_amount=Decimal(str(data.get("discount_amount", "0"))),
            sale_net_amount=Decimal(str(data.get("sale_net_amount", "0"))),
        )

    # ── bot-base consumer seam (B1) ──────────────────────────────────────────
    def get_bot_commands(self) -> List["BotCommand"]:
        """The referral command, surfaced only when the plugin is enabled.

        Returns ``[]`` otherwise (Liskov — a disabled feature contributes no
        commands). The neutral ``BotCommand`` DTO is imported lazily so this
        module loads even when bot-base is absent.
        """
        from vbwd.plugins.base import PluginStatus

        if self.status != PluginStatus.ENABLED:
            return []

        from plugins.bot_base.bot_base.types import BotCommand

        return [
            BotCommand(
                name=NEW_COUPON_COMMAND,
                description="Mint a personal referral coupon you can share",
                namespace=BOT_NAMESPACE,
            )
        ]

    def handle_action(self, context: "BotInbound") -> "BotReply":
        """Handle ``/referral_program_new_coupon`` for an identified issuer."""
        from plugins.bot_base.bot_base.types import BotReply

        if context.identity is None:
            return BotReply(
                text="Please connect your account first, then try again."
            )

        issuer_user_id = context.identity.vbwd_user_id
        user = self._resolve_user(issuer_user_id)
        if user is None or not user.has_permission(GENERATE_PERMISSION):
            return BotReply(
                text=(
                    "You do not have permission to generate referral coupons. "
                    "Ask an admin to grant you the "
                    f"'{GENERATE_PERMISSION}' permission."
                )
            )

        prefix = self._parse_flag(context.args, "--coupon")
        if not prefix:
            return BotReply(
                text=(
                    "Usage: /referral_program_new_coupon --coupon <PREFIX> "
                    "[--template <CODE>]"
                )
            )
        template_code = self._parse_flag(context.args, "--template")

        nickname = self._resolve_nickname(issuer_user_id)
        if not nickname:
            return BotReply(
                text=(
                    "Please set a nickname first, then run this command again — "
                    "your nickname becomes part of the coupon code."
                )
            )

        from plugins.referral.referral.service_factory import build_referral_service
        from plugins.referral.referral.services.referral_service import ReferralError

        service = build_referral_service()
        try:
            referral_coupon = service.mint(
                issuer_user_id=issuer_user_id,
                issuer_nickname=nickname,
                raw_prefix=prefix,
                template_coupon_code=template_code,
            )
        except ReferralError as error:
            return BotReply(text=str(error))

        return BotReply(
            text=(
                f"Your referral coupon is ready: {referral_coupon.coupon_code}\n"
                "Share it — when someone checks out with it, you earn a token "
                "commission."
            )
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_flag(args: List[str], flag: str) -> Optional[str]:
        """Return the value following ``flag`` in ``args`` (``None`` if absent)."""
        for index, token in enumerate(args):
            if token == flag and index + 1 < len(args):
                return args[index + 1]
        return None

    @staticmethod
    def _resolve_user(user_id):
        container = getattr(current_app, "container", None)
        if container is None:
            return None
        return container.user_repository().find_by_id(user_id)

    @staticmethod
    def _resolve_nickname(user_id) -> Optional[str]:
        from vbwd.extensions import db
        from plugins.meinchat.meinchat.repositories.nickname_repository import (
            NicknameRepository,
        )

        nickname_row = NicknameRepository(db.session).find_by_user_id(user_id)
        return nickname_row.nickname if nickname_row else None
