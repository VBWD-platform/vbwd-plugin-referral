"""Referral plugin admin routes — program settings + masked statistics (S92 B3).

Absolute ``/api/v1/admin/referral/*`` paths (the plugin exposes no other prefix
group, but absolute paths keep this open to a public group later). Each route is
``@require_auth @require_admin @require_permission(...)``:

- ``GET  /settings``           (``referral.view``)   — current program settings.
- ``PUT  /settings``           (``referral.manage``) — upsert the singleton.
- ``GET  /coupons``            (``referral.view``)   — masked stats list,
  paginated + quick-search + sortable + filterable.
- ``POST /coupons/bulk-delete``(``referral.manage``) — delete by ids.

Masking is authoritative in :meth:`ReferralService.stats_query` (a full unused
code never leaves the service); pagination / search / sort / filter are applied
here over the already-masked rows.
"""
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from flask import Blueprint, jsonify, request
from vbwd.middleware.auth import require_admin, require_auth, require_permission

from plugins.referral.referral.models.referral_coupon import ReferralCommissionType
from plugins.referral.referral.service_factory import build_referral_service

referral_bp = Blueprint("referral", __name__)

#: Stats columns the admin may sort by (whitelist — never trust raw input).
_SORTABLE_FIELDS = frozenset(
    {"issuer_nickname", "coupon_code", "status", "issued_at", "used_at"}
)
_DEFAULT_PER_PAGE = 25
_MAX_PER_PAGE = 200


# ── settings ─────────────────────────────────────────────────────────────


@referral_bp.route("/api/v1/admin/referral/settings", methods=["GET"])
@require_auth
@require_admin
@require_permission("referral.view")
def admin_get_referral_settings():
    """Return the current referral program settings (singleton)."""
    service = build_referral_service()
    settings = service.get_settings()
    return jsonify({"settings": _settings_to_dict(settings)}), 200


@referral_bp.route("/api/v1/admin/referral/settings", methods=["PUT"])
@require_auth
@require_admin
@require_permission("referral.manage")
def admin_update_referral_settings():
    """Upsert the singleton settings — commission type/value + template ids."""
    data = request.get_json() or {}

    try:
        commission_type = ReferralCommissionType(data.get("commission_type"))
    except ValueError:
        return (
            jsonify(
                {
                    "error": "Invalid commission_type",
                    "allowed": [member.value for member in ReferralCommissionType],
                }
            ),
            400,
        )

    commission_value = _parse_decimal(data.get("commission_value"))
    if commission_value is None:
        return jsonify({"error": "commission_value must be a number"}), 400

    selected_template_coupon_ids = [
        str(value) for value in (data.get("selected_template_coupon_ids") or [])
    ]

    service = build_referral_service()
    settings = service.set_settings(
        commission_type=commission_type,
        commission_value=commission_value,
        selected_template_coupon_ids=selected_template_coupon_ids,
    )
    from vbwd.extensions import db

    db.session.commit()
    return jsonify({"settings": _settings_to_dict(settings)}), 200


# ── stats list ───────────────────────────────────────────────────────────


@referral_bp.route("/api/v1/admin/referral/coupons", methods=["GET"])
@require_auth
@require_admin
@require_permission("referral.view")
def admin_list_referral_coupons():
    """List referral coupons as masked stat rows (paginated, searchable)."""
    service = build_referral_service()
    rows = service.stats_query()  # masking already applied server-side

    rows = _apply_filters(rows, request.args)
    rows = _apply_sort(
        rows,
        sort_field=request.args.get("sort"),
        order=request.args.get("order", "desc"),
    )

    total = len(rows)
    page, per_page = _parse_pagination(request.args)
    start = (page - 1) * per_page
    paged = rows[start : start + per_page]

    return (
        jsonify(
            {
                "coupons": paged,
                "total": total,
                "page": page,
                "per_page": per_page,
            }
        ),
        200,
    )


@referral_bp.route("/api/v1/admin/referral/coupons/bulk-delete", methods=["POST"])
@require_auth
@require_admin
@require_permission("referral.manage")
def admin_bulk_delete_referral_coupons():
    """Delete referral stat rows by id."""
    data = request.get_json() or {}
    ids = [str(value) for value in (data.get("ids") or [])]
    if not ids:
        return jsonify({"error": "ids is required"}), 400

    service = build_referral_service()
    deleted = service.delete_coupons(ids)
    from vbwd.extensions import db

    db.session.commit()
    return jsonify({"deleted": deleted}), 200


# ── helpers ──────────────────────────────────────────────────────────────


def _settings_to_dict(settings) -> dict:
    return {
        "commission_type": settings.commission_type.value,
        "commission_value": str(settings.commission_value),
        "selected_template_coupon_ids": list(
            settings.selected_template_coupon_ids or []
        ),
    }


def _parse_decimal(raw_value) -> Optional[Decimal]:
    if raw_value is None:
        return None
    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, ValueError):
        return None


def _apply_filters(rows: List[dict], args) -> List[dict]:
    """Quick-search + status / issuer / date-issued / date-used filters."""
    status = args.get("status")
    if status:
        rows = [row for row in rows if row.get("status") == status]

    issuer = args.get("issuer")
    if issuer:
        rows = [row for row in rows if row.get("issuer_user_id") == issuer]

    issued_from = args.get("issued_from")
    if issued_from:
        rows = [
            row
            for row in rows
            if row.get("issued_at") and row["issued_at"] >= issued_from
        ]

    used_from = args.get("used_from")
    if used_from:
        rows = [
            row for row in rows if row.get("used_at") and row["used_at"] >= used_from
        ]

    search = (args.get("search") or "").strip().lower()
    if search:
        rows = [row for row in rows if _matches_search(row, search)]

    return rows


def _matches_search(row: dict, search: str) -> bool:
    for field in ("issuer_nickname", "coupon_code", "status"):
        value = row.get(field)
        if value and search in str(value).lower():
            return True
    return False


def _apply_sort(rows: List[dict], *, sort_field: Optional[str], order: str) -> List[dict]:
    if not sort_field or sort_field not in _SORTABLE_FIELDS:
        sort_field = "issued_at"
    reverse = order.lower() != "asc"
    return sorted(rows, key=lambda row: (row.get(sort_field) or ""), reverse=reverse)


def _parse_pagination(args) -> tuple:
    try:
        page = max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(args.get("per_page", _DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = _DEFAULT_PER_PAGE
    per_page = max(1, min(per_page, _MAX_PER_PAGE))
    return page, per_page
