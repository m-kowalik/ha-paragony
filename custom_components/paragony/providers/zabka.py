"""Żappka: logowanie SMS + e-paragony (nieoficjalne API aplikacji)."""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime

import aiohttp

from ..const import CHAIN_ZABKA
from ..jpk import decode_jws_payload, parse_document
from ..models import Receipt
from .base import ProviderAuthError, ProviderError, ReceiptProvider

GOOGLE_API_KEY = "AIzaSyDe2Fgxn_8HJ6NrtJtp69YqXwocutAoa9Q"
IDENTITY_URL = "https://www.googleapis.com/identitytoolkit/v3/relyingparty"
SECURE_TOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={GOOGLE_API_KEY}"
SUPER_ACCOUNT_URL = "https://super-account.spapp.zabka.pl/"
API_URL = "https://api.spapp.zabka.pl/"

ANDROID_HEADERS = {
    "X-Android-Package": "pl.zabka.apb2c",
    "X-Android-Cert": "FAB089D9E5B41002F29848FC8034A391EE177077",
}
USER_AGENT = "okhttp/4.12.0"
COUNTRY_CODE = "48"

Q_SEND_CODE = (
    "mutation SendVerificationCode($input: SendVerificationCodeInput!) "
    "{ sendVerificationCode(input: $input) { retryAfterSeconds } }"
)
Q_SIGN_IN_PHONE = "mutation SignInWithPhone($input: SignInInput!) { signIn(input: $input) { customToken } }"
Q_SIGN_IN = (
    "mutation SignIn($signInInput: SignInInput!) { signIn(signInInput: $signInInput) "
    "{ profile { __typename } } }"
)
Q_EPRINTS = (
    "query EPrints($cursor: String) { ePrints(after: $cursor) { ePrints { id type address "
    "{ streetAddress city } createdAt paymentAmount { amount currencyCode fractionDigits } "
    "htmlUrl jsonUrl pdfUrl } pagination { cursor } } }"
)
Q_AUTHORIZE_PARTNER = (
    "mutation AuthorizePartner($input: AuthorizePartnerInput!) "
    "{ authorizePartner(authorizePartnerInput: $input) { partnerIdToken } }"
)
MAX_PAGES = 100


def _jwt_exp(token: str) -> float:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))


def normalize_phone(phone: str) -> str:
    """Zwraca 9-cyfrowy numer krajowy (usuwa spacje, myślniki i prefiks +48)."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith(COUNTRY_CODE):
        digits = digits[2:]
    return digits


class ZabkaProvider(ReceiptProvider):
    chain = CHAIN_ZABKA

    def __init__(self, session: aiohttp.ClientSession, refresh_token: str | None = None) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._id_token: str | None = None
        self._partner_token: str | None = None
        self._anon_token: str | None = None
        self._eprints: dict[str, dict] = {}

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    # --- HTTP ---------------------------------------------------------------

    async def _post(self, url: str, body: dict, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if "googleapis.com" in url:
            headers.update(ANDROID_HEADERS)
        try:
            async with self._session.post(
                url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderError(f"Błąd połączenia z {url}: {err}") from err
        if status in (401, 403) or (status == 400 and "securetoken" in url):
            raise ProviderAuthError(f"Odmowa autoryzacji ({status})")
        if status >= 400:
            raise ProviderError(f"HTTP {status} z {url}")
        return data or {}

    async def _gql(self, url: str, query: str, variables: dict | None = None, token: str | None = None) -> dict:
        data = await self._post(url, {"query": query, "variables": variables or {}}, token)
        if errors := data.get("errors"):
            codes = {(e.get("extensions") or {}).get("code") for e in errors}
            message = "; ".join(e.get("message", "") for e in errors)
            if codes & {"UNAUTHENTICATED", "FORBIDDEN"}:
                raise ProviderAuthError(message)
            raise ProviderError(message)
        return data.get("data") or {}

    # --- logowanie ----------------------------------------------------------

    async def async_send_code(self, phone: str) -> int:
        """Wysyła SMS z kodem. Zwraca liczbę sekund do możliwej ponownej wysyłki."""
        res = await self._post(
            f"{IDENTITY_URL}/signupNewUser?key={GOOGLE_API_KEY}", {"clientType": "CLIENT_TYPE_ANDROID"}
        )
        self._anon_token = res["idToken"]
        data = await self._gql(
            SUPER_ACCOUNT_URL,
            Q_SEND_CODE,
            {"input": {"phoneNumber": {"countryCode": COUNTRY_CODE, "nationalNumber": normalize_phone(phone)}}},
            self._anon_token,
        )
        return int((data.get("sendVerificationCode") or {}).get("retryAfterSeconds") or 0)

    async def async_sign_in(self, phone: str, code: str) -> str:
        """Kończy logowanie kodem z SMS. Zwraca refresh token."""
        if self._anon_token is None:
            raise ProviderError("Najpierw wyślij kod SMS")
        try:
            data = await self._gql(
                SUPER_ACCOUNT_URL,
                Q_SIGN_IN_PHONE,
                {
                    "input": {
                        "phoneNumber": {"countryCode": COUNTRY_CODE, "nationalNumber": normalize_phone(phone)},
                        "verificationCode": code.strip(),
                    }
                },
                self._anon_token,
            )
            custom_token = data["signIn"]["customToken"]
        except (KeyError, TypeError, ProviderError) as err:
            raise ProviderAuthError("Nieprawidłowy kod SMS") from err
        res = await self._post(
            f"{IDENTITY_URL}/verifyCustomToken?key={GOOGLE_API_KEY}",
            {"token": custom_token, "returnSecureToken": True},
        )
        self._id_token = res["idToken"]
        self._refresh_token = res["refreshToken"]
        await self._gql(API_URL, Q_SIGN_IN, {"signInInput": {"sessionId": str(uuid.uuid4())}}, self._id_token)
        return self._refresh_token

    async def _async_id_token(self) -> str:
        if self._id_token and _jwt_exp(self._id_token) - time.time() > 120:
            return self._id_token
        if not self._refresh_token:
            raise ProviderAuthError("Brak refresh tokenu")
        res = await self._post(
            SECURE_TOKEN_URL, {"grantType": "refresh_token", "refreshToken": self._refresh_token}
        )
        if "access_token" not in res:
            raise ProviderAuthError("Nie udało się odświeżyć tokenu")
        self._id_token = res["access_token"]
        self._refresh_token = res.get("refresh_token", self._refresh_token)
        return self._id_token

    async def _async_partner_token(self) -> str:
        """Token usługi „nano” — wymagany do pobrania treści e-paragonu."""
        if self._partner_token and _jwt_exp(self._partner_token) - time.time() > 60:
            return self._partner_token
        data = await self._gql(
            API_URL, Q_AUTHORIZE_PARTNER, {"input": {"partnerIdentity": "nano"}}, await self._async_id_token()
        )
        self._partner_token = data["authorizePartner"]["partnerIdToken"]
        return self._partner_token

    # --- paragony -----------------------------------------------------------

    async def async_list_receipt_ids(self) -> list[str]:
        cursor: str | None = None
        self._eprints = {}
        for _ in range(MAX_PAGES):
            data = await self._gql(API_URL, Q_EPRINTS, {"cursor": cursor}, await self._async_id_token())
            page = data.get("ePrints") or {}
            prints = page.get("ePrints") or []
            for eprint in prints:
                self._eprints[eprint["id"]] = eprint
            next_cursor = (page.get("pagination") or {}).get("cursor")
            if not prints or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return list(self._eprints)

    async def async_get_receipt(self, receipt_id: str) -> Receipt:
        eprint = self._eprints.get(receipt_id)
        if eprint is None:
            await self.async_list_receipt_ids()
            eprint = self._eprints[receipt_id]
        headers = {
            "Authorization": f"Bearer {await self._async_partner_token()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        try:
            async with self._session.get(
                eprint["jsonUrl"], headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status in (401, 403):
                    self._partner_token = None
                    raise ProviderAuthError(f"Odmowa dostępu do paragonu ({resp.status})")
                if resp.status >= 400:
                    raise ProviderError(f"HTTP {resp.status} przy pobieraniu paragonu")
                raw = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderError(f"Błąd pobierania paragonu: {err}") from err
        return build_receipt(eprint, raw)


def build_receipt(eprint: dict, raw: dict) -> Receipt:
    """Łączy wpis z listy ePrints z treścią receipt.json."""
    doc = parse_document(decode_jws_payload(raw["data"]))
    purchased_at = datetime.fromisoformat((doc["purchased_at"] or eprint["createdAt"]).replace("Z", "+00:00"))
    address = eprint.get("address") or {}
    total = doc["total"]
    if total is None:
        total = int((eprint.get("paymentAmount") or {}).get("amount", 0))
    return Receipt(
        chain=CHAIN_ZABKA,
        external_id=eprint["id"],
        purchased_at=purchased_at,
        total=total,
        currency=doc["currency"],
        store_name=doc["store_name"],
        store_address=doc["store_address"]
        or ", ".join(part for part in (address.get("streetAddress"), address.get("city")) if part),
        items=doc["items"],
        raw={k: v for k, v in raw.items() if k != "header"},  # nagłówek zawiera imię kasjera
    )
