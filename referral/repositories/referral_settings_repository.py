"""Data access for the singleton referral_settings row."""
from typing import Optional

from plugins.referral.referral.models.referral_settings import ReferralSettings


class ReferralSettingsRepository:
    """Thin wrapper for the single ReferralSettings row."""

    def __init__(self, session) -> None:
        self._session = session

    def get_singleton(self) -> Optional[ReferralSettings]:
        """Return the one settings row, or ``None`` if not yet created."""
        return self._session.query(ReferralSettings).first()

    def save(self, settings: ReferralSettings) -> ReferralSettings:
        self._session.add(settings)
        self._session.flush()
        return settings
