#!/usr/bin/env python3
"""Rozpoznanie API Lidl Plus pod kątem e-paragonów.

Użycie:
  lidl_discover.py url                  # link do logowania (PKCE); otwórz w przeglądarce
  lidl_discover.py code <url|kod>       # wymienia kod z przekierowania com.lidlplus.app://callback na tokeny
  lidl_discover.py list                 # lista paragonów (v2) → .secrets/lidl/tickets.json
  lidl_discover.py fetch [N]            # szczegóły N najnowszych paragonów (v3) → .secrets/lidl/<id>.json

Po zalogowaniu przeglądarka nie otworzy adresu com.lidlplus.app://…, więc kod trzeba
skopiować z narzędzi deweloperskich (zakładka Sieć, nagłówek Location ostatniego przekierowania).
Tokeny trzymane wyłącznie w .secrets/lidl_tokens.json (chmod 600), nigdy nie są wypisywane.
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

AUTH = "https://accounts.lidl.com"
TICKETS = "https://tickets.lidlplus.com/api"
CLIENT_ID = "LidlPlusNativeClient"
REDIRECT_URI = "com.lidlplus.app://callback"
COUNTRY = "PL"
LANGUAGE = "pl"

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
TOKENS = SECRETS / "lidl_tokens.json"
OUT = SECRETS / "lidl"


def save_tokens(data: dict) -> None:
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    TOKENS.write_text(json.dumps(data))
    os.chmod(TOKENS, 0o600)


def load_tokens() -> dict:
    return json.loads(TOKENS.read_text()) if TOKENS.exists() else {}


def token_request(payload: dict) -> dict:
    secret = base64.b64encode(f"{CLIENT_ID}:secret".encode()).decode()
    req = urllib.request.Request(
        f"{AUTH}/connect/token",
        urllib.parse.urlencode(payload).encode(),
        {"Authorization": f"Basic {secret}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        sys.exit(f"HTTP {err.code} z /connect/token: {err.read().decode(errors='replace')[:300]}")


def access_token() -> str:
    tokens = load_tokens()
    if not tokens.get("refresh_token"):
        sys.exit("Brak refresh tokenu — najpierw: url + code")
    res = token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    tokens["refresh_token"] = res.get("refresh_token", tokens["refresh_token"])
    save_tokens(tokens)
    return res["access_token"]


def get(url: str, token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "App-Version": "16.45.5",
        "Operating-System": "iOs",
        "App": "com.lidl.eci.lidl.plus",
        "Accept-Language": LANGUAGE,
    }
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        sys.exit(f"HTTP {err.code} z {url}: {err.read().decode(errors='replace')[:300]}")


def cmd_url() -> None:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    save_tokens({**load_tokens(), "verifier": verifier})
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile offline_access lpprofile lpapis",
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "Country": COUNTRY,
        "language": f"{LANGUAGE}-{COUNTRY}",
    }
    print(f"{AUTH}/connect/authorize?{urllib.parse.urlencode(params)}")


def cmd_code(value: str) -> None:
    match = re.search(r"code=([^&\s]+)", value)
    code = urllib.parse.unquote(match.group(1)) if match else value.strip()
    tokens = load_tokens()
    res = token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": tokens.get("verifier", ""),
        }
    )
    save_tokens({"refresh_token": res["refresh_token"]})
    print(f"OK — zapisano refresh token, access token ważny {res.get('expires_in')} s")


def cmd_list() -> list[dict]:
    token = access_token()
    url = f"{TICKETS}/v2/{COUNTRY}/tickets"
    first = get(f"{url}?pageNumber=1&onlyFavorite=false", token)
    tickets = first.get("tickets", [])
    for page in range(2, first["totalCount"] // max(first["size"], 1) + 2):
        tickets += get(f"{url}?pageNumber={page}&onlyFavorite=false", token).get("tickets", [])
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    (OUT / "tickets.json").write_text(json.dumps({**first, "tickets": tickets}, ensure_ascii=False, indent=2))
    print(f"{len(tickets)} paragonów; klucze: {sorted(tickets[0]) if tickets else '-'}")
    return tickets


def cmd_fetch(limit: int) -> None:
    tickets = cmd_list()
    token = access_token()
    for ticket in tickets[:limit]:
        data = get(f"{TICKETS}/v3/{COUNTRY}/tickets/{ticket['id']}", token)
        (OUT / f"{ticket['id']}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"{ticket['id']}: klucze {sorted(data)}")


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
