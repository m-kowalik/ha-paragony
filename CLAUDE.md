# Paragony — przewodnik dla Claude Code

Custom integration Home Assistant pobierająca e-paragony z aplikacji sieci handlowych do lokalnej bazy SQLite (`/config/paragony.db`). Obsługiwana sieć: Żabka (Żappka). W planach: Lidl Plus, Biedronka.

## Architektura (`custom_components/paragony/`)
- `models.py`: wspólne `Receipt` / `ReceiptItem`. **Kwoty zawsze w groszach (int).** `ReceiptItem.kind` to `product` albo `deposit` (kaucja).
- `jpk.py`: parser e-paragonu w formacie MF `JPK_KASA_PARAGON_v2-0`, podawanego jako JWS (`header.payload.sig`, czytamy tylko payload).
  - `towar` to pozycja, a `rabat` następuje po towarze, którego dotyczy (`wart < 0` oznacza rabat, `> 0` narzut).
  - `oper: true` oznacza storno i jest pomijane.
  - Kaucje są w `opak.daneOpak`.
  - `clean_name` usuwa sufiks stawki VAT (`-A`…`-G`).
  - Parser jest niezależny od sieci, więc użyj go też dla innych sieci, jeśli dają JPK.
- `providers/base.py`: interfejs `ReceiptProvider` (`async_list_receipt_ids`, `async_get_receipt`, `refresh_token`) oraz wyjątki `ProviderAuthError` (koordynator zamienia go na `ConfigEntryAuthFailed`, co uruchamia reauth) i `ProviderError` (zamieniany na `UpdateFailed`).
- `providers/zabka.py`: klient aiohttp. Funkcja `build_receipt(eprint, raw)` łączy wpis z listy z treścią `receipt.json` i usuwa `header` (zawiera imię kasjera).
- `db.py`: `ReceiptDB` z metodami synchronicznymi, w HA wywoływanymi przez `hass.async_add_executor_job`.
  - Deduplikacja przez `UNIQUE(chain, external_id)`.
  - `casefold()` jest zarejestrowane jako funkcja SQLite i służy do wyszukiwania bez rozróżniania wielkości liter.
  - Daty zapisywane w UTC (ISO).
- `coordinator.py`: synchronizacja co 6 h. Pobiera tylko nowe ID. Przy pierwszym imporcie historii **nie** wysyła eventów `paragony_new_receipt`. Zapisuje zrotowany refresh token do `entry.data`.
- `sensor.py`: ostatni zakup (kwota i atrybuty z pozycjami), data ostatniego zakupu, wydatki w bieżącym miesiącu, liczba paragonów.
- `__init__.py`:
  - akcje `paragony.search` (`SupportsResponse.ONLY`; filtry: `product`, `date_from`/`date_to` w lokalnej strefie, `chain`, `include_deposits`, `limit`) i `paragony.sync`;
  - jedna wspólna instancja bazy w `hass.data[DOMAIN]["db"]`.
- `config_flow.py`: numer telefonu → kod SMS → `entry.data = {chain, phone, refresh_token}`. `unique_id` ma postać `zabka_<numer>`. Ma też krok reauth.

## Nieoficjalne API Żappki
Nazwy operacji i schemat pochodzą z APK (stringi Apollo w `classes*.dex`). Introspekcja jest wyłączona.

1. Firebase `identitytoolkit/v3/relyingparty/signupNewUser` daje anonimowy `idToken`.
   - Wszystkie wywołania `googleapis.com` **wymagają** nagłówków `X-Android-Package: pl.zabka.apb2c` i `X-Android-Cert: FAB089D9E5B41002F29848FC8034A391EE177077`, inaczej dostaniesz 403.
2. `https://super-account.spapp.zabka.pl/`: `SendVerificationCode` (SMS), potem `SignInWithPhone`, który zwraca `customToken`.
3. `verifyCustomToken` zwraca `idToken` i `refreshToken`. Odświeżanie przez `securetoken.googleapis.com/v1/token`.
4. `https://api.spapp.zabka.pl/` (GraphQL, `Authorization: Bearer <idToken>`):
   - `SignIn` rejestruje sesję;
   - `query EPrints($cursor)` zwraca listę paragonów (`id`, `createdAt`, `paymentAmount`, `jsonUrl`, `pdfUrl`, `htmlUrl`);
   - `mutation AuthorizePartner(input: {partnerIdentity: "nano"})` zwraca `partnerIdToken`.
5. `GET jsonUrl` z `Authorization: Bearer <partnerIdToken>` zwraca `{data: <JWS z JPK>, body: [...], header: [...]}`.

Wskazówka diagnostyczna: API maskuje błędy walidacji (`400 GRAPHQL_VALIDATION_FAILED`, bez podpowiedzi). Poprawne pole odpytane anonimowym tokenem daje natomiast `403 FORBIDDEN`, więc można tak sprawdzać, czy pole istnieje.

## Dodawanie nowej sieci
1. Dodaj stałą sieci w `const.py` (`CHAIN_*`, `CHAIN_NAMES`).
2. Napisz `providers/<siec>.py` implementujący `ReceiptProvider`.
3. Dodaj krok w `config_flow.py` i gałąź w `async_setup_entry`.
4. Dopisz sieć do selektora `chain` w `services.yaml` oraz do tłumaczeń (`strings.json`, `translations/*.json`).

Tropy:
- **Lidl Plus:** biblioteka `lidl-plus` (Andre0512), `tickets()` / `ticket(id)`. Logowanie wymaga przeglądarki, więc w config flow przyjmij refresh token.
- **Biedronka:** moja.biedronka.pl pozwala pobrać e-paragony jako JSON, prawdopodobnie w tym samym formacie JPK. Endpointy i logowanie trzeba dopiero rozpoznać.

## Testy
```bash
python3 -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest
```
- `tests/conftest.py` ładuje moduły jako pakiet `paragony` bez wykonywania `__init__.py`, więc testy parsera i bazy działają bez HA.
- `tests/test_integration.py` uruchamia prawdziwy HA z podmienionym providerem. Katalog konfiguracji jest przestawiony na `tmp_path`, bo inaczej `paragony.db` zostaje w `testing_config` biblioteki.
- `tests/fixtures/zabka_receipt.json` jest **syntetyczny**. Nie commituj prawdziwych paragonów ani numerów telefonów.

## Narzędzia
- `tools/zabka_discover.py`: ręczne rozpoznawanie API (`send`, `verify`, `introspect`, `probe`, `query`). Tokeny trafiają do `.secrets/` (gitignored, chmod 600).

## Konwencje
- Komunikaty, docstringi i komentarze po polsku; dopasuj się do stylu istniejącego kodu.
- Bez zewnętrznych zależności w `manifest.json` (aiohttp pochodzi z HA).
- Nie loguj tokenów.
