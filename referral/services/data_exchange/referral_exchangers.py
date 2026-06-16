"""Referral entity exchanger for the S46 data-exchange seam (S92 B4).

Exposes ``referral_coupons`` (``ReferralCoupon``, natural key ``coupon_code``)
through the core ``EntityExchanger`` contract so referral statistics appear on
the generic Settings → Import/Export page and the per-list export controls,
clustered under ``sales`` alongside the discount exchangers.

**Masking on export (Decision D-ExportMask):** a still-unused row exports the
MASKED ``coupon_code`` and a ``used`` row exports the full code — the SAME rule
the on-screen stats apply, so a CSV/JSON export can never leak a live unused
code. The mask is the single shared :func:`coupon_code.mask_code` that
``ReferralService.stats_query`` also calls (DRY — one mask implementation).

Design notes:

* **Reused perms** — maps ``export_permission`` / ``import_permission`` onto the
  plugin's existing ``referral.view`` / ``referral.manage`` (single source).
* **DRY** — reuses :class:`BaseModelExchanger`; only the narrow
  ``_SessionModelRepository`` adapter and the ``_serialise_row`` mask override
  are added (mirrors the discount / CMS pattern).
* **No core change** — registration happens in ``ReferralPlugin.on_enable``
  through the shared ``db.session``; core imports no ``plugins.*`` module.

Engineering requirements (binding, restated): TDD-first; DevOps-first; SOLID
(one exchanger per entity, narrow ports); DI (session injected); DRY; Liskov
(the mask-overriding subtype yields the same row shape — only the code value is
masked, exactly as production stats are); clean code; no overengineering.
Quality guard: ``bin/pre-commit-check.sh --plugin referral --full``.
"""
from typing import Any, List, Optional

from vbwd.services.data_exchange.base_model_exchanger import BaseModelExchanger
from vbwd.services.data_exchange.port import CLUSTER_SALES, EntityExchanger
from vbwd.services.data_exchange.registry import data_exchange_registry

from plugins.referral.referral.models.referral_coupon import (
    ReferralCoupon,
    ReferralCouponStatus,
)
from plugins.referral.referral.services.coupon_code import mask_code

# Existing referral permissions (single source — ReferralPlugin.admin_permissions).
PERM_REFERRAL_VIEW = "referral.view"
PERM_REFERRAL_MANAGE = "referral.manage"

# The exported/imported columns. ``coupon_code`` is the natural key; ``status``
# drives the export mask. The NOT NULL FKs ``issuer_user_id`` / ``coupon_id`` are
# carried so a same-instance round-trip reconstructs a valid row (mirrors the
# discount coupon exchanger preserving its ``discount_id`` FK); they are opaque
# UUIDs and leak nothing the mask protects.
_PUBLIC_FIELDS = [
    "coupon_code",
    "issuer_user_id",
    "coupon_id",
    "issuer_nickname",
    "commission_type",
    "commission_value",
    "status",
    "discount_amount",
    "commission_tokens_paid",
]


class _SessionModelRepository:
    """Narrow model repo satisfying the ``BaseModelExchanger`` contract (ISP).

    Mirrors the discount adapter: the referral repository exposes domain finders
    rather than the four flat methods the base exchanger needs.
    """

    def __init__(self, session: Any, model_class: type, natural_key: str) -> None:
        self._session = session
        self._model_class = model_class
        self._natural_key = natural_key

    def find_all(self) -> List[Any]:
        return self._session.query(self._model_class).all()

    def find_by_natural_key(self, value: Any) -> Optional[Any]:
        column = getattr(self._model_class, self._natural_key)
        return self._session.query(self._model_class).filter(column == value).first()

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    def delete_all(self) -> None:
        self._session.query(self._model_class).delete()


class ReferralCouponsExchanger(BaseModelExchanger):
    """``referral_coupons`` exchanger that masks unused codes on export.

    Permissions map onto the plugin's existing ``referral.view`` /
    ``referral.manage``. The only behavioural addition over
    :class:`BaseModelExchanger` is the export mask: a row whose status is not
    ``used`` exports the masked ``coupon_code`` (D-ExportMask), reusing the same
    :func:`mask_code` the stats read uses.
    """

    @property
    def export_permission(self) -> str:
        return PERM_REFERRAL_VIEW

    @property
    def import_permission(self) -> str:
        return PERM_REFERRAL_MANAGE

    def _serialise_row(self, row: Any, *, include_pii: bool) -> dict:
        """Serialise a row, masking the code of any not-yet-used coupon.

        Delegates to the base serialiser (single source of the field-stripping /
        enum-flattening logic), then applies the privacy mask to ``coupon_code``
        when the row is unused — the exact rule the on-screen stats apply, via
        the one shared :func:`mask_code` (no duplicated mask).
        """
        serialised = super()._serialise_row(row, include_pii=include_pii)
        if getattr(row, "status", None) != ReferralCouponStatus.USED:
            serialised["coupon_code"] = mask_code(row.coupon_code)
        return serialised


def build_referral_exchangers(session: Any) -> List[EntityExchanger]:
    """Construct the referral exchangers bound to ``session``."""
    return [
        ReferralCouponsExchanger(
            entity_key="referral_coupons",
            label="Referral Coupons",
            cluster=CLUSTER_SALES,
            natural_key="coupon_code",
            model_class=ReferralCoupon,
            repository=_SessionModelRepository(
                session, ReferralCoupon, "coupon_code"
            ),
            session=session,
            public_fields=_PUBLIC_FIELDS,
            supported_formats=frozenset({"json", "csv"}),
        )
    ]


def register_referral_exchangers(session: Any) -> None:
    """Register the referral exchangers into the registry (idempotent).

    Called from ``ReferralPlugin.on_enable``. Re-registering replaces by key, so
    a repeat enable (per-test app) is clear-safe.
    """
    for exchanger in build_referral_exchangers(session):
        data_exchange_registry.register(exchanger)
