"""Integration specs for the referral admin routes (S92 B3).

Drives the FULL runtime path through the Flask client against real PG:

- settings round-trip (``GET`` reflects what ``PUT`` persisted, including the
  selected template ids);
- the stats list applies the authoritative server-side masking
  (``status != used`` → masked code, ``status == used`` → full code);
- the stats list supports quick-search, status/issuer filters, sort, and
  pagination;
- ``bulk-delete`` removes rows by id;
- permission gating — 401 unauthenticated, 403 for a non-admin user.

Users/templates are made through the service/repo layer (no raw SQL). Coupons
are minted + redeemed through the real ``ReferralService`` so masking is exercised
on genuine issued vs. used rows.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from vbwd.models.enums import InvoiceStatus, UserRole, UserStatus
from vbwd.models.invoice import UserInvoice
from vbwd.models.user import User

from plugins.discount.discount.checkout_adjustment import checkout_price_adjustment
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

SETTINGS_PATH = "/api/v1/admin/referral/settings"
COUPONS_PATH = "/api/v1/admin/referral/coupons"
BULK_DELETE_PATH = "/api/v1/admin/referral/coupons/bulk-delete"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register(app, email):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    existing = user_repo.find_by_email(email)
    if existing is None:
        auth_service.register(email=email, password="RefAdmin123@")
        db.session.commit()
        existing = user_repo.find_by_email(email)
    result = auth_service.login(email=email, password="RefAdmin123@")
    return existing.id, result.token


def _make_admin(app, email):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_id, token = _register(app, email)
    user = UserRepository(db.session).find_by_id(user_id)
    user.role = UserRole.ADMIN  # legacy fallback: ADMIN ⇒ all permissions
    db.session.commit()
    return user_id, token


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


def _mint(session, *, issuer, nickname="Bob", template=None):
    template = template or _seed_template(session)
    service = build_referral_service(session)
    service.set_settings(
        commission_type=ReferralCommissionType.ABSOLUTE_TOKENS,
        commission_value=Decimal("50"),
        selected_template_coupon_ids=[str(template.id)],
    )
    return service.mint(
        issuer_user_id=issuer.id, issuer_nickname=nickname, raw_prefix="REF_USER_"
    )


def _redeem(session, *, code, buyer, subtotal="100.00"):
    result = checkout_price_adjustment(
        code=code,
        subtotal=Decimal(subtotal),
        user_id=str(buyer.id),
        scope="SUBSCRIPTION",
        currency="EUR",
    )
    assert result.valid is True
    invoice = UserInvoice(
        id=uuid4(),
        user_id=buyer.id,
        invoice_number=f"INV-{uuid4().hex[:8]}",
        amount=Decimal(subtotal),
        currency="EUR",
        status=InvoiceStatus.PAID,
    )
    session.add(invoice)
    session.commit()
    result.on_committed(str(invoice.id), str(buyer.id))


# ── settings round-trip ──────────────────────────────────────────────────


def test_get_settings_returns_defaults(app, client):
    _admin_id, token = _make_admin(app, "ref-admin-1@example.com")
    resp = client.get(SETTINGS_PATH, headers=_auth(token))
    assert resp.status_code == 200
    body = resp.get_json()["settings"]
    assert "commission_type" in body
    assert "commission_value" in body
    assert "selected_template_coupon_ids" in body


def test_put_settings_persists_and_round_trips(app, client, db):
    _admin_id, token = _make_admin(app, "ref-admin-2@example.com")
    template = _seed_template(db.session)

    put = client.put(
        SETTINGS_PATH,
        json={
            "commission_type": "percent_of_sale",
            "commission_value": "12.5",
            "selected_template_coupon_ids": [str(template.id)],
        },
        headers=_auth(token),
    )
    assert put.status_code == 200

    get = client.get(SETTINGS_PATH, headers=_auth(token))
    body = get.get_json()["settings"]
    assert body["commission_type"] == "percent_of_sale"
    assert Decimal(str(body["commission_value"])) == Decimal("12.5")
    assert body["selected_template_coupon_ids"] == [str(template.id)]


# ── stats list + masking ─────────────────────────────────────────────────


def test_stats_masks_unused_reveals_used(app, client, db):
    _admin_id, token = _make_admin(app, "ref-admin-3@example.com")
    template = _seed_template(db.session)
    issuer = _seed_user(db.session)
    buyer = _seed_user(db.session)

    unused = _mint(db.session, issuer=issuer, template=template)
    used = _mint(db.session, issuer=issuer, template=template)
    _redeem(db.session, code=used.coupon_code, buyer=buyer)

    resp = client.get(COUPONS_PATH, headers=_auth(token))
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.get_json()["coupons"]}

    # Unused: masked — never the full live code.
    assert rows[str(unused.id)]["coupon_code"] != unused.coupon_code
    assert "x" in rows[str(unused.id)]["coupon_code"]
    # Used: full code revealed (it is spent).
    assert rows[str(used.id)]["coupon_code"] == used.coupon_code
    assert rows[str(used.id)]["commission_tokens_paid"] == 50
    # Unused: no money / tokens columns yet.
    assert rows[str(unused.id)]["discount_amount"] is None
    assert rows[str(unused.id)]["commission_tokens_paid"] is None


def test_stats_filter_by_status(app, client, db):
    _admin_id, token = _make_admin(app, "ref-admin-4@example.com")
    template = _seed_template(db.session)
    issuer = _seed_user(db.session)
    buyer = _seed_user(db.session)

    _mint(db.session, issuer=issuer, template=template)
    used = _mint(db.session, issuer=issuer, template=template)
    _redeem(db.session, code=used.coupon_code, buyer=buyer)

    resp = client.get(f"{COUPONS_PATH}?status=used", headers=_auth(token))
    rows = resp.get_json()["coupons"]
    assert all(row["status"] == "used" for row in rows)
    assert any(row["id"] == str(used.id) for row in rows)


def test_stats_quick_search_by_issuer_nickname(app, client, db):
    _admin_id, token = _make_admin(app, "ref-admin-5@example.com")
    template = _seed_template(db.session)
    issuer = _seed_user(db.session)

    minted = _mint(db.session, issuer=issuer, nickname="Zelda", template=template)

    resp = client.get(f"{COUPONS_PATH}?search=zeld", headers=_auth(token))
    rows = resp.get_json()["coupons"]
    assert any(row["id"] == str(minted.id) for row in rows)
    assert all("zeld" in row["issuer_nickname"].lower() for row in rows)


def test_stats_sort_and_pagination(app, client, db):
    _admin_id, token = _make_admin(app, "ref-admin-6@example.com")
    template = _seed_template(db.session)
    issuer = _seed_user(db.session)
    for _index in range(3):
        _mint(db.session, issuer=issuer, template=template)

    resp = client.get(
        f"{COUPONS_PATH}?sort=issued_at&order=asc&page=1&per_page=2",
        headers=_auth(token),
    )
    body = resp.get_json()
    assert len(body["coupons"]) == 2
    assert body["total"] >= 3


# ── bulk delete ──────────────────────────────────────────────────────────


def test_bulk_delete_removes_rows(app, client, db):
    _admin_id, token = _make_admin(app, "ref-admin-7@example.com")
    template = _seed_template(db.session)
    issuer = _seed_user(db.session)
    first_id = str(_mint(db.session, issuer=issuer, template=template).id)
    second_id = str(_mint(db.session, issuer=issuer, template=template).id)

    resp = client.post(
        BULK_DELETE_PATH,
        json={"ids": [first_id]},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 1

    listing = client.get(COUPONS_PATH, headers=_auth(token))
    remaining = {row["id"] for row in listing.get_json()["coupons"]}
    assert first_id not in remaining
    assert second_id in remaining


# ── permission gating ────────────────────────────────────────────────────


def test_settings_requires_auth(app, client):
    assert client.get(SETTINGS_PATH).status_code == 401


def test_coupons_requires_auth(app, client):
    assert client.get(COUPONS_PATH).status_code == 401


def test_non_admin_forbidden(app, client, db):
    _user_id, token = _register(app, "ref-plain-user@example.com")
    resp = client.get(COUPONS_PATH, headers=_auth(token))
    assert resp.status_code == 403
