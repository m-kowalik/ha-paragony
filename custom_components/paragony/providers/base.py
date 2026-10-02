"""Wspólny interfejs dostawców paragonów."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Receipt


class ProviderError(Exception):
    """Błąd komunikacji z API sieci."""


class ProviderAuthError(ProviderError):
    """Sesja wygasła lub dane logowania są nieprawidłowe — potrzebne ponowne logowanie."""


class ReceiptProvider(ABC):
    chain: str

    @property
    @abstractmethod
    def refresh_token(self) -> str | None:
        """Aktualny refresh token (może się zmieniać po odświeżeniu)."""

    @abstractmethod
    async def async_list_receipt_ids(self) -> list[str]:
        """Identyfikatory wszystkich dostępnych paragonów."""

    @abstractmethod
    async def async_get_receipt(self, receipt_id: str) -> Receipt:
        """Pełny paragon z pozycjami."""
