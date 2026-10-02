#!/usr/bin/env python3
"""Rozpoznanie API Moja Biedronka pod kątem e-paragonów.

Użycie:
  biedronka_discover.py url                  # link do logowania (Keycloak, PKCE); otwórz w przeglądarce
  biedronka_discover.py code <url|kod>       # wymienia kod z przekierowania app://cma20.biedronka.pl na tokeny
  biedronka_discover.py list                 # lista transakcji → .secrets/biedronka/transactions.json
  biedronka_discover.py fetch [N]            # e-paragon (JSON) + szczegóły N najnowszych transakcji

Po zalogowaniu (numer telefonu, captcha, SMS) przeglądarka nie otworzy adresu app://cma20.biedronka.pl?code=…,
więc trzeba go skopiować z paska adresu albo z DevTools (zakładka Sieć, nagłówek Location). Kod Keycloak
jest ważny około minuty.
Tokeny trzymane wyłącznie w .secrets/biedronka_tokens.json (chmod 600), nigdy nie są wypisywane.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KEYCLOAK = "https://konto.biedronka.pl/realms/loyalty/protocol/openid-connect"
API = "https://api.prod.biedronka.cloud/api/v7"
CLIENT_ID = "cma20"
REDIRECT_URI = "app://cma20.biedronka.pl"
USER_AGENT = "Android/2.22.2"
# Cloudflare przed Keycloakiem odrzuca domyślny User-Agent urllib (błąd 1010)
KEYCLOAK_USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
TOKENS = SECRETS / "biedronka_tokens.json"
OUT = SECRETS / "biedronka"


def save_tokens(data: dict) -> None:
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    TOKENS.write_text(json.dumps(data))
    os.chmod(TOKENS, 0o600)


def load_tokens() -> dict:
    return json.loads(TOKENS.read_text()) if TOKENS.exists() else {}


def token_request(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{KEYCLOAK}/token",
        urllib.parse.urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, **payload}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": KEYCLOAK_USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        sys.exit(f"HTTP {err.code} z /token: {err.read().decode(errors='replace')[:300]}")


def access_token() -> str:
    tokens = load_tokens()
    if not tokens.get("refresh_token"):
        sys.exit("Brak refresh tokenu — najpierw: url + code")
    res = token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    tokens["refresh_token"] = res.get("refresh_token", tokens["refresh_token"])
    save_tokens(tokens)
    return res["access_token"]


def get(path: str, token: str, params: dict | None = None, headers: dict | None = None):
    url = f"{API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept-Language": "pl-PL",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        return {"_http_status": err.code, "_body": err.read().decode(errors="replace")[:500]}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_text": body.decode(errors="replace")}


def cmd_url() -> None:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    save_tokens({**load_tokens(), "verifier": verifier})
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    print(f"{KEYCLOAK}/auth?{urllib.parse.urlencode(params)}")


def cmd_code(value: str) -> None:
    match = re.search(r"code=([^&\s]+)", value)
    code = urllib.parse.unquote(match.group(1)) if match else value.strip()
    tokens = load_tokens()
    res = token_request(
        {"grant_type": "authorization_code", "code": code, "code_verifier": tokens.get("verifier", "")}
    )
    save_tokens({"refresh_token": res["refresh_token"]})
    print(
        f"OK — zapisano refresh token; access ważny {res.get('expires_in')} s, "
        f"refresh {res.get('refresh_expires_in')} s"
    )


def cmd_list() -> list[dict]:
    token = access_token()
    first = get("transactions/", token, {"page": 1})
    transactions = list(first.get("transactions") or [])
    for page in range(2, int(first.get("page_count") or 1) + 1):
        transactions += get("transactions/", token, {"page": page}).get("transactions") or []
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    (OUT / "transactions.json").write_text(
        json.dumps({**first, "transactions": transactions}, ensure_ascii=False, indent=2)
    )
    print(
        f"{len(transactions)} transakcji; klucze strony: {sorted(first)}; "
        f"klucze transakcji: {sorted(transactions[0]) if transactions else '-'}"
    )
    return transactions


def cmd_fetch(limit: int) -> None:
    transactions = cmd_list()
    token = access_token()
    for tx in transactions[:limit]:
        tx_id = tx["id"]
        details = get(f"transactions/{tx_id}/", token)
        receipt = get(f"transactions/{tx_id}/e-receipt/", token, headers={"output-format": "json"})
        (OUT / f"{tx_id}.details.json").write_text(json.dumps(details, ensure_ascii=False, indent=2))
        (OUT / f"{tx_id}.receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        describe = lambda d: sorted(d) if isinstance(d, dict) else type(d).__name__  # noqa: E731
        print(f"{tx_id}: e-paragon={tx.get('is_e_receipt_available')} szczegóły {describe(details)} e-receipt {describe(receipt)}")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["url"]:
        cmd_url()
    elif args[:1] == ["code"] and len(args) == 2:
        cmd_code(args[1])
    elif args[:1] == ["list"]:
        cmd_list()
    elif args[:1] == ["fetch"]:
        cmd_fetch(int(args[1]) if len(args) > 1 else 5)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
