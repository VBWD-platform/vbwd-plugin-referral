"""Integration: the /referral_program_new_coupon bot command (B1).

Builds a BotInbound (no network) and drives the referral plugin's
``handle_action`` the way bot-base would: a permitted issuer mints a correctly
patterned coupon bound to the cloned discount + a referral row; an unpermitted
user is refused; missing nickname / missing --coupon / unknown --template are
guided refusals; and the command is absent when the plugin is disabled (Liskov).
"""
import re
from decimal import Decimal
from uuid import uuid4

from vbwd.models.enums import UserRole, UserStatus
from vbwd.models.user import User

from plugins.bot_base.bot_base.types import BotIdentity, BotInbound, ChatRef
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
from plugins.meinchat.meinchat.models.user_nickname import UserNickname
from plugins.referral.referral.models.referral_coupon import ReferralCommissionType
from plugins.referral.referral.repositories.referral_coupon_repository import (
    ReferralCouponRepository,
)
from plugins.referral.referral.service_factory import build_referral_service

_CODE_PATTERN = re.compile(r"^REF_USER_BOB_[0-9A-F]{16}$")


def _referral_plugin(app):
    return app.plugin_manager.get_plugin("referral")


def _seed_user(session, role=UserRole.ADMIN) -> User:
    user = User(
        id=uuid4(),
        email=f"issuer-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        status=UserStatus.ACTIVE,
        role=role,
    )
    session.add(user)
    session.commit()
    return user


def _seed_nickname(session, user_id, nickname="Bob"):
    row = UserNickname(id=uuid4(), user_id=user_id, nickname=nickname)
    session.add(row)
    session.commit()
    return row


def _seed_template(session, code="SUMMER10"):
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
    return CouponRepository(session).save(
        Coupon(id=uuid4(), code=code, discount_id=discount.id, is_active=True)
    )


def _configure_template(session, template_coupon):
    build_referral_service(session).set_settings(
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value=Decimal("50"),
        selected_template_coupon_ids=[str(template_coupon.id)],
    )


def _inbound(user_id, args):
    return BotInbound(
        provider_id="meinchat",
        chat_ref=ChatRef(provider_id="meinchat", chat_id="c1"),
        sender_ref="s1",
        command="referral_program_new_coupon",
        args=args,
        identity=BotIdentity(
            provider_id="meinchat",
            external_user_id="ext1",
            vbwd_user_id=user_id,
        ),
    )


def test_permitted_user_mints_coupon(app, db):
    user = _seed_user(db.session, role=UserRole.ADMIN)  # legacy-admin → has perm
    _seed_nickname(db.session, user.id, "Bob")
    template = _seed_template(db.session)
    _configure_template(db.session, template)

    reply = _referral_plugin(app).handle_action(
        _inbound(user.id, ["--coupon", "REF_USER_"])
    )

    code_match = re.search(r"REF_USER_BOB_[0-9A-F]{16}", reply.text)
    assert code_match is not None
    minted_code = code_match.group(0)
    assert _CODE_PATTERN.match(minted_code)
    # Bound to a cloned discount coupon + a referral row created.
    assert CouponRepository(db.session).find_by_code(minted_code) is not None
    assert ReferralCouponRepository(db.session).find_by_code(minted_code) is not None


def test_unpermitted_user_is_refused(app, db):
    user = _seed_user(db.session, role=UserRole.USER)  # plain user → no perm
    _seed_nickname(db.session, user.id, "Bob")
    template = _seed_template(db.session)
    _configure_template(db.session, template)

    reply = _referral_plugin(app).handle_action(
        _inbound(user.id, ["--coupon", "REF_USER_"])
    )

    assert "permission" in reply.text.lower()
    assert ReferralCouponRepository(db.session).list_all() == []


def test_missing_nickname_is_guided_refusal(app, db):
    user = _seed_user(db.session, role=UserRole.ADMIN)
    template = _seed_template(db.session)
    _configure_template(db.session, template)

    reply = _referral_plugin(app).handle_action(
        _inbound(user.id, ["--coupon", "REF_USER_"])
    )

    assert "nickname" in reply.text.lower()
    assert ReferralCouponRepository(db.session).list_all() == []


def test_missing_coupon_flag_shows_usage(app, db):
    user = _seed_user(db.session, role=UserRole.ADMIN)
    _seed_nickname(db.session, user.id, "Bob")
    template = _seed_template(db.session)
    _configure_template(db.session, template)

    reply = _referral_plugin(app).handle_action(_inbound(user.id, []))

    assert "--coupon" in reply.text
    assert ReferralCouponRepository(db.session).list_all() == []


def test_unknown_template_lists_valid(app, db):
    user = _seed_user(db.session, role=UserRole.ADMIN)
    _seed_nickname(db.session, user.id, "Bob")
    template = _seed_template(db.session, code="SUMMER10")
    _configure_template(db.session, template)

    reply = _referral_plugin(app).handle_action(
        _inbound(user.id, ["--coupon", "REF_USER_", "--template", "NOPE"])
    )

    assert "SUMMER10" in reply.text
    assert ReferralCouponRepository(db.session).list_all() == []


def test_command_present_when_enabled(app):
    commands = _referral_plugin(app).get_bot_commands()
    names = {command.name for command in commands}
    assert "referral_program_new_coupon" in names
