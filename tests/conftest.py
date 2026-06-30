"""Shared fixtures for referral plugin tests.

Unit specs use in-memory fakes (no DB). Integration specs request ``app`` /
``client`` and self-bootstrap a ``<dbname>_test`` database with core + discount +
meinchat + referral tables created via ``db.create_all()`` (mirrors the
bot_meinchat / discount harness). Enables discount + meinchat + referral so each
plugin's ``on_enable`` registrations fire even in an isolated per-plugin CI clone
(no plugins.json present there).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("TESTING", "true")


def _test_db_url() -> str:
    base = os.getenv("DATABASE_URL", "postgresql://vbwd:vbwd@postgres:5432/vbwd")
    prefix, _, dbname = base.rpartition("/")
    dbname = dbname.split("?")[0]
    return f"{prefix}/{dbname}_test"


def _ensure_test_db(url: str) -> None:
    from sqlalchemy import create_engine, text

    main_url = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1].split("?")[0]
    engine = create_engine(main_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        engine.dispose()


def _ensure_referral_commission_enum_value(database) -> None:
    """Add ``REFERRAL_COMMISSION`` to the native ``tokentransactiontype`` enum.

    The test harness builds schema via ``create_all`` (not migrations), so the
    core migration that adds this enum value never runs against the persistent
    ``<dbname>_test`` DB. ``create_all`` will not ADD a value to an already
    existing native enum. We replicate the core migration's idempotent
    ``ADD VALUE IF NOT EXISTS`` here (autocommit — ``ALTER TYPE … ADD VALUE``
    cannot run in a transaction) so the test DB matches production. Test-infra
    only — no core file is touched.
    """
    from sqlalchemy import text

    engine = database.engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text(
                "ALTER TYPE tokentransactiontype "
                "ADD VALUE IF NOT EXISTS 'REFERRAL_COMMISSION'"
            )
        )


def _ensure_referral_enabled(flask_app) -> None:
    """Enable discount + meinchat + referral so on_enable registrations fire.

    A fresh per-plugin CI clone has no plugins.json, so plugins are
    discovered-but-not-enabled; discount must be enabled first (referral clones
    its coupons) and meinchat for the nickname. Idempotent.
    """
    from vbwd.plugins.base import PluginStatus

    manager = getattr(flask_app, "plugin_manager", None)
    if manager is None:
        return
    with flask_app.app_context():
        for plugin_name in ("discount", "meinchat", "referral"):
            plugin = manager.get_plugin(plugin_name)
            if plugin is None or plugin.status == PluginStatus.ENABLED:
                continue
            try:
                manager.enable_plugin(plugin_name)
            except ValueError:
                if plugin.status == PluginStatus.INITIALIZED:
                    plugin.enable()


@pytest.fixture(scope="session")
def app():
    from vbwd.app import create_app
    from vbwd.extensions import db as _db

    test_url = _test_db_url()
    _ensure_test_db(test_url)
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": test_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
            "RATELIMIT_STORAGE_URL": "memory://",
        }
    )
    with application.app_context():
        import plugins.discount.discount.models  # noqa: F401
        import plugins.meinchat.meinchat.models  # noqa: F401
        import plugins.referral.referral.models  # noqa: F401

        from vbwd.testing.integration_db import ensure_schema_and_baseline

        ensure_schema_and_baseline(_db)
        _ensure_referral_commission_enum_value(_db)

    _ensure_referral_enabled(application)

    yield application

    with application.app_context():
        _db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _isolate_test(app, request):
    """Isolate every test in a rolled-back transaction (self-cleaning, no wipe)."""
    from vbwd.extensions import db as _db

    if request.node.get_closest_marker("no_db_isolation") is not None:
        with app.app_context():
            yield
            _db.session.remove()
        return

    with app.app_context():
        from vbwd.testing.integration_db import rollback_isolation

        with rollback_isolation(_db):
            yield


@pytest.fixture
def db(_isolate_test):
    from vbwd.extensions import db as _db

    yield _db
