"""S92 B0 — migration up/down/up for the referral tables (real PG).

Runs the migration through alembic's Operations context in a rolled-back
transaction. The conftest ``create_all`` already created the tables + enums, so
they are dropped first to exercise a clean upgrade. Validates the chain anchors
on the discount head (the FK target ``discount_coupon`` lives there) and the rev
id is ≤ 32 chars.
"""
import importlib.util
import os

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

pytestmark = pytest.mark.no_db_isolation


def _load_migration():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "versions",
        "20260615_1100_referral_tables.py",
    )
    spec = importlib.util.spec_from_file_location("referral_tables", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
TABLES = ("referral_coupon", "referral_settings")
ENUMS = ("referral_commission_type_enum", "referral_coupon_status_enum")


def _has_table(connection, table) -> bool:
    return inspect(connection).has_table(table)


def _drop_referral_objects(operations, connection) -> None:
    for table in TABLES:
        if _has_table(connection, table):
            operations.drop_table(table)
    for enum_name in ENUMS:
        connection.execute(text(f'DROP TYPE IF EXISTS "{enum_name}"'))


@pytest.fixture
def migration_connection(app):
    from vbwd.extensions import db

    connection = db.engine.connect()
    transaction = connection.begin()
    operations = Operations(MigrationContext.configure(connection))
    _drop_referral_objects(operations, connection)
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.integration
def test_revision_anchors_on_discount_head_and_id_is_short():
    assert migration.revision == "20260615_1100_referral"
    assert migration.down_revision == "20260531_discount_prefix"
    assert len(migration.revision) <= 32


@pytest.mark.integration
def test_up_down_up(migration_connection):
    connection = migration_connection
    for table in TABLES:
        assert not _has_table(connection, table)

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.upgrade()
    for table in TABLES:
        assert _has_table(connection, table)

    with Operations.context(context):
        migration.downgrade()
    for table in TABLES:
        assert not _has_table(connection, table)

    with Operations.context(context):
        migration.upgrade()
    for table in TABLES:
        assert _has_table(connection, table)
