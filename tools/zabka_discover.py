#!/usr/bin/env python3
"""Rozpoznanie API Żappki pod kątem e-paragonów (etap 0).

Użycie:
  zabka_discover.py send <numer>            # wysyła SMS z kodem (numer bez +48)
  zabka_discover.py verify <numer> <kod>    # loguje i zapisuje tokeny w .secrets/
  zabka_discover.py introspect              # próba pobrania schematu GraphQL
  zabka_discover.py probe                   # zgadywanie pól (podpowiedzi „Did you mean”)
  zabka_discover.py query <plik.graphql> [zmienne.json] [--super]

Tokeny trzymane wyłącznie w .secrets/zabka_tokens.json (chmod 600), nigdy nie są wypisywane.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GOOGLE_API_KEY = "AIzaSyDe2Fgxn_8HJ6NrtJtp69YqXwocutAoa9Q"
IDENTITY = "https://www.googleapis.com/identitytoolkit/v3/relyingparty"
SECURE_TOKEN = f"https://securetoken.googleapis.com/v1/token?key={GOOGLE_API_KEY}"
SUPER_ACCOUNT = "https://super-account.spapp.zabka.pl/"
API = "https://api.spapp.zabka.pl/"

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
TOKENS = SECRETS / "zabka_tokens.json"
OUT = SECRETS / "discovery"

ANDROID_HEADERS = {
    "X-Android-Package": "pl.zabka.apb2c",
    "X-Android-Cert": "FAB089D9E5B41002F29848FC8034A391EE177077",
}


def post(url: str, body: dict, token: str | None = None, extra: dict | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "okhttp/4.12.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "googleapis.com" in url:
        headers.update(ANDROID_HEADERS)
    headers.update(extra or {})
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        payload = err.read().decode(errors="replace")
        try:
            return {"_http_status": err.code, **json.loads(payload)}
        except json.JSONDecodeError:
            return {"_http_status": err.code, "_body": payload[:2000]}


def save_tokens(data: dict) -> None:
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    TOKENS.write_text(json.dumps(data))
    os.chmod(TOKENS, 0o600)


def load_tokens() -> dict:
    if not TOKENS.exists():
        sys.exit("Brak tokenów — najpierw: send + verify")
    return json.loads(TOKENS.read_text())


def jwt_exp(token: str) -> int:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["exp"]


def anonymous_token() -> str:
    res = post(f"{IDENTITY}/signupNewUser?key={GOOGLE_API_KEY}", {"clientType": "CLIENT_TYPE_ANDROID"})
    return res["idToken"]


def id_token() -> str:
    """Zwraca ważny idToken, w razie potrzeby odświeżając go refresh tokenem."""
    tokens = load_tokens()
    if jwt_exp(tokens["id_token"]) - time.time() > 120:
        return tokens["id_token"]
    res = post(
        SECURE_TOKEN,
        {"grantType": "refresh_token", "refreshToken": tokens["refresh_token"]},
    )
    if "access_token" not in res:
        sys.exit(f"Odświeżenie tokenu nie powiodło się: {res.get('error', res)}")
    tokens.update(id_token=res["access_token"], refresh_token=res["refresh_token"])
    save_tokens(tokens)
    return tokens["id_token"]


def cmd_send(phone: str) -> None:
    anon = anonymous_token()
    res = post(
        SUPER_ACCOUNT,
        {
            "operationName": "SendVerificationCode",
            "query": "mutation SendVerificationCode($input: SendVerificationCodeInput!) "
            "{ sendVerificationCode(input: $input) { retryAfterSeconds } }",
            "variables": {"input": {"phoneNumber": {"countryCode": "48", "nationalNumber": phone}}},
        },
        anon,
    )
    save_tokens({"anon": anon})
    print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_verify(phone: str, code: str) -> None:
    anon = load_tokens()["anon"]
    res = post(
        SUPER_ACCOUNT,
        {
            "operationName": "SignInWithPhone",
            "query": "mutation SignInWithPhone($input: SignInInput!) { signIn(input: $input) { customToken } }",
            "variables": {
                "input": {
                    "phoneNumber": {"countryCode": "48", "nationalNumber": phone},
                    "verificationCode": code,
                }
            },
        },
        anon,
    )
    try:
        custom = res["data"]["signIn"]["customToken"]
    except (KeyError, TypeError):
        sys.exit(f"Błędny kod lub odpowiedź: {json.dumps(res, ensure_ascii=False)[:500]}")
    res = post(
        f"{IDENTITY}/verifyCustomToken?key={GOOGLE_API_KEY}",
        {"token": custom, "returnSecureToken": True},
    )
    save_tokens({"id_token": res["idToken"], "refresh_token": res["refreshToken"]})
    res = post(
        API,
        {
            "operationName": "SignIn",
            "query": "mutation SignIn($signInInput: SignInInput!) { signIn(signInInput: $signInInput) "
            "{ profile { __typename } } }",
            "variables": {"signInInput": {"sessionId": str(uuid.uuid4())}},
        },
        res["idToken"],
    )
    print("Zalogowano. SignIn:", json.dumps(res, ensure_ascii=False)[:300])


def gql(query: str, variables: dict | None = None, super_account: bool = False) -> dict:
    body = {"query": query, "variables": variables or {}}
    return post(SUPER_ACCOUNT if super_account else API, body, id_token())


def dump(name: str, data: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


INTROSPECTION = """
query { __schema { queryType { name } mutationType { name }
  types { kind name fields(includeDeprecated: true) { name args { name type { kind name ofType { kind name ofType { kind name } } } }
    type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } } } }
"""
KEYWORDS = ("receipt", "paragon", "transaction", "purchase", "basket", "order", "shopping", "history")


def cmd_introspect() -> None:
    for label, sup in (("api", False), ("super-account", True)):
        res = gql(INTROSPECTION, super_account=sup)
        path = dump(f"introspection-{label}.json", res)
        types = (res.get("data") or {}).get("__schema", {}).get("types")
        if not types:
            print(f"[{label}] introspekcja niedostępna: {json.dumps(res, ensure_ascii=False)[:300]}")
            continue
        print(f"[{label}] {len(types)} typów → {path}")
        for t in types:
            hits = [f["name"] for f in t.get("fields") or [] if any(k in f["name"].lower() for k in KEYWORDS)]
            if any(k in (t["name"] or "").lower() for k in KEYWORDS) or hits:
                print(f"  {t['name']}: {hits}")


GUESSES = [
    "receipts", "receipt", "eReceipts", "eReceipt", "digitalReceipts", "purchaseHistory", "purchases",
    "transactions", "transactionHistory", "shoppingHistory", "orders", "orderHistory", "paragony",
    "history", "baskets", "loyaltyTransactions", "pointsHistory",
]


def cmd_probe() -> None:
    for sup in (False, True):
        label = "super-account" if sup else "api"
        for field in GUESSES:
            res = gql(f"query {{ {field} {{ __typename }} }}", super_account=sup)
            errs = [e.get("message", "") for e in res.get("errors", [])]
            print(f"[{label}] {field}: {'OK' if res.get('data') else ' | '.join(errs)[:250]}")


def cmd_query(path: str, variables: str | None, super_account: bool) -> None:
    query = Path(path).read_text()
    vars_ = json.loads(Path(variables).read_text()) if variables else {}
    res = gql(query, vars_, super_account)
    out = dump(f"query-{Path(path).stem}.json", res)
    print(json.dumps(res, indent=2, ensure_ascii=False)[:4000])
    print(f"\n→ pełna odpowiedź: {out}")


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--super"]
    sup = "--super" in sys.argv
    if not args:
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    if cmd == "send":
        cmd_send(*rest)
    elif cmd == "verify":
        cmd_verify(*rest)
    elif cmd == "introspect":
        cmd_introspect()
    elif cmd == "probe":
        cmd_probe()
    elif cmd == "query":
        cmd_query(rest[0], rest[1] if len(rest) > 1 else None, sup)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
