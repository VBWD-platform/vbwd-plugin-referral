"""S92 B0 — referral plugin tables (referral_coupon + referral_settings).

Anchored on the discount plugin's head (``20260531_discount_prefix``) because
``referral_coupon.coupon_id`` FKs ``discount_coupon`` — the target table must
exist first. The referral plugin declares ``discount`` as a dependency, so the
discount migrations are always present when these run.

Guarded + idempotent (``IF NOT EXISTS`` paths) so it is safe on a create_all dev
DB, a fresh CI DB, and re-runs.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260615_1100_referral"
down_revision = "20260531_discount_prefix"
branch_labels = None
depends_on = None

_COMMISSION_TYPE_ENUM = "referral_commission_type_enum"
_COUPON_STATUS_ENUM = "referral_coupon_status_enum"


def _table_exists(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def upgrade() -> None:
    conn = op.get_bind()

    commission_type_enum = postgresql.ENUM(
        "percent_of_sale",
        "absolute_tokens",
        name=_COMMISSION_TYPE_ENUM,
        create_type=False,
    )
    coupon_status_enum = postgresql.ENUM(
        "issued",
        "used",
        "expired",
        name=_COUPON_STATUS_ENUM,
        create_type=False,
    )
    commission_type_enum.create(conn, checkfirst=True)
    coupon_status_enum.create(conn, checkfirst=True)

    if not _table_exists(conn, "referral_settings"):
        op.create_table(
            "referral_settings",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
            ),
            sa.Column("commission_type", commission_type_enum, nullable=False),
            sa.Column("commission_value", sa.Numeric(12, 4), nullable=False),
            sa.Column(
                "selected_template_coupon_ids",
                postgresql.JSONB,
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
        )

    if not _table_exists(conn, "referral_coupon"):
        op.create_table(
            "referral_coupon",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
            ),
            sa.Column(
                "issuer_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "coupon_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("discount_coupon.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("coupon_code", sa.String(120), nullable=False),
            sa.Column("issuer_nickname", sa.String(64), nullable=False),
            sa.Column(
                "template_coupon_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
            sa.Column("commission_type", commission_type_enum, nullable=False),
            sa.Column("commission_value", sa.Numeric(12, 4), nullable=False),
            sa.Column("status", coupon_status_enum, nullable=False),
            sa.Column("issued_at", sa.DateTime(), nullable=True),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("discount_amount", sa.Numeric(12, 2), nullable=True),
            sa.Column("commission_tokens_paid", sa.Integer(), nullable=True),
            sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint(
                "coupon_id", "invoice_id", name="uq_referral_coupon_invoice"
            ),
        )
        op.create_index(
            "ix_referral_coupon_issuer_user_id",
            "referral_coupon",
            ["issuer_user_id"],
        )
        op.create_index(
            "ix_referral_coupon_coupon_id", "referral_coupon", ["coupon_id"]
        )
        op.create_index(
            "ix_referral_coupon_coupon_code", "referral_coupon", ["coupon_code"]
        )
        op.create_index("ix_referral_coupon_status", "referral_coupon", ["status"])
        op.create_index(
            "ix_referral_coupon_invoice_id", "referral_coupon", ["invoice_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, "referral_coupon"):
        op.drop_table("referral_coupon")
    if _table_exists(conn, "referral_settings"):
        op.drop_table("referral_settings")

    postgresql.ENUM(name=_COUPON_STATUS_ENUM, create_type=False).drop(
        conn, checkfirst=True
    )
    postgresql.ENUM(name=_COMMISSION_TYPE_ENUM, create_type=False).drop(
        conn, checkfirst=True
    )
