"""Wpis „Papierowe paragony”: paragony trafiają do bazy akcją paragony.add_from_image, nie z API."""
from __future__ import annotations

from ..const import CHAIN_PHOTO
from ..models import Receipt
from .base import ProviderError, ReceiptProvider


class PhotoProvider(ReceiptProvider):
    chain = CHAIN_PHOTO

    @property
    def refresh_token(self) -> str | None:
        return None

    async def async_list_receipt_ids(self) -> list[str]:
        return []

    async def async_get_receipt(self, receipt_id: str) -> Receipt:
        raise ProviderError("Paragony ze zdjęć dodaje się akcją paragony.add_from_image")
