"""Parser e-paragonu w formacie MF JPK_KASA_PARAGON (używany m.in. przez Żabkę)."""
from __future__ import annotations

import base64
import json
import re

from .models import ReceiptItem

_VAT_SUFFIX = re.compile(r"-[A-G]$")


def decode_jws_payload(token: str) -> dict:
    """Zwraca część payload z JWS bez weryfikacji podpisu."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def clean_name(raw: str) -> str:
    """Usuwa sufiks stawki VAT (np. „LAYS MAX 120g-C” → „LAYS MAX 120g”)."""
    return _VAT_SUFFIX.sub("", raw.strip()).strip()


def _quantity(value) -> float:
    return float(str(value).replace(",", "."))


def parse_items(paragon: dict) -> list[ReceiptItem]:
    """Zamienia pozycje paragonu na listę ReceiptItem.

    Rabat w JPK występuje jako osobna pozycja tuż po towarze, którego dotyczy.
    Pozycje z oper=true to storno i są pomijane.
    """
    items: list[ReceiptItem] = []
    for position in paragon.get("pozycja", []):
        if (towar := position.get("towar")) is not None:
            if towar.get("oper"):
                continue
            items.append(
                ReceiptItem(
                    name=clean_name(towar["nazwa"]),
                    raw_name=towar["nazwa"],
                    quantity=_quantity(towar.get("ilosc", 1)),
                    unit=towar.get("jm"),
                    unit_price=int(towar.get("cena", towar["brutto"])),
                    total_price=int(towar["brutto"]),
                )
            )
        elif (rabat := position.get("rabat")) is not None:
            if rabat.get("oper") or not items:
                continue
            # wart < 0 oznacza rabat, > 0 narzut
            items[-1].discount -= int(rabat["wart"])

    for pack in (paragon.get("opak") or {}).get("daneOpak", []):
        quantity = _quantity(pack.get("ilosc", 1))
        price = int(pack.get("cena", 0))
        items.append(
            ReceiptItem(
                name=pack["nazwa"],
                raw_name=pack["nazwa"],
                quantity=quantity,
                unit="szt.",
                unit_price=price,
                total_price=round(price * quantity),
                kind="deposit",
            )
        )
    return items


def parse_document(payload: dict) -> dict:
    """Wyciąga z dokumentu JPK dane potrzebne do Receipt."""
    dokument = payload["dokument"]
    paragon = dokument["paragon"]
    seller = dokument.get("podmiot1", {})
    address = seller.get("adresPod") or {}
    street = " ".join(
        part for part in (address.get("ulica"), address.get("nrDomu"), address.get("nrLok")) if part
    )
    city = " ".join(part for part in (address.get("kodPoczt"), address.get("miejsc")) if part)
    total = (paragon.get("total") or {}).get("zaplZwrot")
    if total is None:
        total = (paragon.get("podsum") or {}).get("sumaBrutto")
    return {
        "purchased_at": paragon.get("zakSprzed") or dokument.get("naglowek", {}).get("dataJPK"),
        "store_name": seller.get("nazwaPod"),
        "store_address": ", ".join(part for part in (street, city) if part) or None,
        "total": int(total) if total is not None else None,
        "currency": (paragon.get("podsum") or {}).get("waluta", "PLN"),
        "items": parse_items(paragon),
    }
