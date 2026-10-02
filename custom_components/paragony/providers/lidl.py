"""Lidl Plus: logowanie OAuth (PKCE) + e-paragony (nieoficjalne API aplikacji).

Paragony występują w dwóch formatach:
- NATIVE (starsze): JSON z listą ``itemsLine``,
- HTML (nowsze): ``htmlPrintedReceipt`` — wydruk paragonu z atrybutami ``data-*`` w liniach pozycji.
"""
from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
import json
import re
import secrets
import time
from datetime import datetime
from urllib.parse import unquote, urlencode
from zoneinfo import ZoneInfo

import aiohttp

from ..const import CHAIN_LIDL
from ..models import Receipt, ReceiptItem
from .base import ProviderAuthError, ProviderError, ReceiptProvider

AUTH_URL = "https://accounts.lidl.com"
TICKETS_URL = "https://tickets.lidlplus.com/api"
CLIENT_ID = "LidlPlusNativeClient"
REDIRECT_URI = "com.lidlplus.app://callback"
SCOPES = "openid profile offline_access lpprofile lpapis"
COUNTRY = "PL"
LANGUAGE = "pl"
APP_VERSION = "16.45.5"
# daty w API to czas lokalny sklepu (lista błędnie dokleja +00:00)
STORE_TZ = ZoneInfo("Europe/Warsaw")
MAX_PAGES = 100

_QTY_LINE = re.compile(r"^\s*([\d,.]+)\s*(kg)?\s*[x*]\s*[\d.,]+\s+(-?[\d.,]+)\s+[A-Z]\s*$")
_DEPOSIT_LINE = re.compile(r"^\s*(.+?)\s+(\d+)\s*\*\s*([\d.,]+)\s+(-?[\d.,]+)\s*$")
_AMOUNT_END = re.compile(r"(-?[\d.,]+)\s*$")


def _grosze(value) -> int:
    """„3,49” / „3.49” / 3.49 → 349."""
    return round(float(str(value).replace(",", ".")) * 100)


def _number(value) -> float:
    return float(str(value).replace(",", "."))


def generate_pkce() -> tuple[str, str]:
    """Zwraca parę (code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login_url(challenge: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "Country": COUNTRY,
        "language": f"{LANGUAGE}-{COUNTRY}",
    }
    return f"{AUTH_URL}/connect/authorize?{urlencode(params)}"


def extract_code(value: str) -> str:
    """Kod autoryzacji z adresu com.lidlplus.app://callback?code=… (albo sam kod)."""
    if match := re.search(r"code=([^&\s]+)", value):
        return unquote(match.group(1))
    return value.strip()


class LidlProvider(ReceiptProvider):
    chain = CHAIN_LIDL

    def __init__(self, session: aiohttp.ClientSession, refresh_token: str | None = None) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def account_id(self) -> str | None:
        """Identyfikator konta (claim ``sub`` z access tokenu)."""
        if not self._access_token:
            return None
        payload = self._access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")

    # --- logowanie ----------------------------------------------------------

    async def _token_request(self, payload: dict) -> None:
        secret = base64.b64encode(f"{CLIENT_ID}:secret".encode()).decode()
        try:
            async with self._session.post(
                f"{AUTH_URL}/connect/token",
                data=payload,
                headers={"Authorization": f"Basic {secret}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None) if status < 500 else {}
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderError(f"Błąd połączenia z Lidl Plus: {err}") from err
        if status in (400, 401) or (status < 400 and "access_token" not in (data or {})):
            # invalid_grant: kod/refresh token wygasł lub został unieważniony
            raise ProviderAuthError(f"Odmowa autoryzacji Lidl Plus ({status})")
        if status >= 400:
            raise ProviderError(f"HTTP {status} z /connect/token")
        self._access_token = data["access_token"]
        self._expires_at = time.monotonic() + float(data.get("expires_in", 0))
        self._refresh_token = data.get("refresh_token", self._refresh_token)

    async def async_exchange_code(self, code: str, verifier: str) -> str:
        """Wymienia kod z przekierowania na tokeny. Zwraca refresh token."""
        await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        )
        assert self._refresh_token is not None
        return self._refresh_token

    async def _async_access_token(self) -> str:
        if self._access_token and self._expires_at - time.monotonic() > 60:
            return self._access_token
        if not self._refresh_token:
            raise ProviderAuthError("Brak refresh tokenu")
        await self._token_request({"grant_type": "refresh_token", "refresh_token": self._refresh_token})
        assert self._access_token is not None
        return self._access_token

    # --- paragony -----------------------------------------------------------

    async def _get(self, url: str) -> dict:
        headers = {
            "Authorization": f"Bearer {await self._async_access_token()}",
            "App-Version": APP_VERSION,
            "Operating-System": "iOs",
            "App": "com.lidl.eci.lidl.plus",
            "Accept-Language": LANGUAGE,
        }
        try:
            async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (401, 403):
                    self._access_token = None
                    raise ProviderAuthError(f"Odmowa dostępu ({resp.status})")
                if resp.status >= 400:
                    raise ProviderError(f"HTTP {resp.status} z {url}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderError(f"Błąd połączenia z {url}: {err}") from err

    async def async_list_receipt_ids(self) -> list[str]:
        url = f"{TICKETS_URL}/v2/{COUNTRY}/tickets"
        ids: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._get(f"{url}?pageNumber={page}&onlyFavorite=false")
            tickets = data.get("tickets") or []
            ids += [ticket["id"] for ticket in tickets]
            if not tickets or len(ids) >= int(data.get("totalCount") or 0):
                break
        return ids

    async def async_get_receipt(self, receipt_id: str) -> Receipt:
        return build_receipt(await self._get(f"{TICKETS_URL}/v3/{COUNTRY}/tickets/{receipt_id}"))


# --- parsowanie --------------------------------------------------------------


def parse_native_items(lines: list[dict]) -> list[ReceiptItem]:
    """Paragon NATIVE: pozycje z ``itemsLine``; kaucja jest atrybutem pozycji."""
    items: list[ReceiptItem] = []
    deposits: list[ReceiptItem] = []
    for line in lines:
        name = line["name"].strip()
        items.append(
            ReceiptItem(
                name=name,
                raw_name=line["name"],
                quantity=_number(line.get("quantity", 1)),
                unit="kg" if line.get("isWeight") else "szt.",
                unit_price=_grosze(line["currentUnitPrice"]),
                total_price=_grosze(line["originalAmount"]),
                discount=sum(_grosze(d["amount"]) for d in line.get("discounts") or []),
            )
        )
        if deposit := line.get("deposit"):
            deposits.append(
                ReceiptItem(
                    name=deposit.get("description") or "Kaucja",
                    raw_name=deposit.get("description") or "Kaucja",
                    quantity=_number(deposit.get("quantity", 1)),
                    unit="szt.",
                    unit_price=_grosze(deposit.get("unitPrice", deposit["amount"])),
                    total_price=_grosze(deposit["amount"]),
                    kind="deposit",
                )
            )
    return items + deposits


class _ReceiptHTMLParser(HTMLParser):
    """Zbiera linie wydruku: (atrybuty spanu, tekst)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[tuple[dict[str, str], str]] = []
        self._current: dict[str, str] | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "span":
            attributes = {key: value or "" for key, value in attrs}
            if "id" in attributes:
                self._current, self._text = attributes, ""

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._current is not None:
            self.lines.append((self._current, self._text))
            self._current = None


def parse_html_items(html: str) -> list[ReceiptItem]:
    """Paragon HTML.

    Każda pozycja to dwie linie ``class="article"`` (nazwa, potem „ilość x cena wartość VAT”);
    linia ``class="discount"`` dotyczy poprzedniej pozycji. Kaucje są w podsumowaniu,
    w sekcji „Opakowania zwrotne wydania”.
    """
    parser = _ReceiptHTMLParser()
    parser.feed(html)
    items: list[ReceiptItem] = []
    deposits: list[ReceiptItem] = []
    in_deposits = False
    for attrs, text in parser.lines:
        css = attrs.get("class", "")
        line_id = attrs.get("id", "")
        if css == "article":
            if not (match := _QTY_LINE.match(text)):
                continue  # linia z nazwą
            name = attrs.get("data-art-description", "").strip()
            items.append(
                ReceiptItem(
                    name=name,
                    raw_name=name,
                    quantity=_number(match.group(1)),
                    unit="kg" if match.group(2) else "szt.",
                    unit_price=_grosze(attrs.get("data-unit-price", "0")),
                    total_price=_grosze(match.group(3)),
                )
            )
        elif css == "discount":
            if items and (match := _AMOUNT_END.search(text)):
                # rabat ujemny; dodatnia wartość oznaczałaby narzut
                items[-1].discount -= _grosze(match.group(1))
        elif line_id.startswith("purchase_summary"):
            lowered = text.strip().lower()
            if lowered.startswith("opakowania zwrotne"):
                in_deposits = "wydania" in lowered
            elif in_deposits and (match := _DEPOSIT_LINE.match(text)):
                deposits.append(
                    ReceiptItem(
                        name=match.group(1).strip(),
                        raw_name=match.group(1).strip(),
                        quantity=_number(match.group(2)),
                        unit="szt.",
                        unit_price=_grosze(match.group(3)),
                        total_price=_grosze(match.group(4)),
                        kind="deposit",
                    )
                )
    return items + deposits


def build_receipt(ticket: dict) -> Receipt:
    """Szczegóły paragonu z API v3 → Receipt."""
    if ticket.get("htmlPrintedReceipt"):
        items = parse_html_items(ticket["htmlPrintedReceipt"])
    else:
        items = parse_native_items(ticket.get("itemsLine") or [])
    purchased_at = datetime.fromisoformat(ticket["date"])
    if purchased_at.tzinfo is None:
        purchased_at = purchased_at.replace(tzinfo=STORE_TZ)
    store = ticket.get("store") or {}
    city = " ".join(part for part in (store.get("postalCode"), store.get("locality")) if part)
    return Receipt(
        chain=CHAIN_LIDL,
        external_id=ticket["id"],
        purchased_at=purchased_at,
        total=_grosze(ticket["totalAmount"]),
        currency=(ticket.get("currency") or {}).get("code", "PLN"),
        store_name=f"Lidl {store['name']}" if store.get("name") else "Lidl",
        store_address=", ".join(part for part in (store.get("address"), city) if part) or None,
        items=items,
        raw={k: v for k, v in ticket.items() if k != "operatorId"},  # identyfikator kasjera
    )
