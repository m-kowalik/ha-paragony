# Paragony — przewodnik dla Claude Code

Custom integration Home Assistant pobierająca e-paragony z aplikacji sieci handlowych do lokalnej bazy SQLite (`/config/paragony.db`). Obsługiwane sieci: Żabka (Żappka), Lidl Plus, a papierowe paragony dowolnej sieci ze zdjęć (AI Task). W planach: Biedronka.

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
- `providers/lidl.py`: klient aiohttp i parser. Funkcja `build_receipt(ticket)` przyjmuje szczegóły paragonu z API v3 i obsługuje dwa formaty:
  - `NATIVE` (starsze): JSON `itemsLine`; rabaty w `discounts[]`, kaucja w `deposit` pozycji;
  - `HTML` (od około 2026): `htmlPrintedReceipt`. Pozycja to dwie linie `class="article"` (nazwa, potem „ilość x cena wartość VAT”), `class="discount"` dotyczy poprzedniej pozycji, a kaucje są w `purchase_summary` po „Opakowania zwrotne wydania”.
  - `date` to czas lokalny sklepu (`Europe/Warsaw`); lista v2 błędnie dokleja `+00:00`.
  - `totalAmount` zawiera kaucje.
  - Z `raw` usuwany jest `operatorId` (identyfikator kasjera).
- `db.py`: `ReceiptDB` z metodami synchronicznymi, w HA wywoływanymi przez `hass.async_add_executor_job`.
  - Deduplikacja przez `UNIQUE(chain, external_id)`.
  - `casefold()` jest zarejestrowane jako funkcja SQLite i służy do wyszukiwania bez rozróżniania wielkości liter.
  - Daty zapisywane w UTC (ISO).
- `coordinator.py`: synchronizacja co 6 h. Pobiera tylko nowe ID. `async_update_local` przelicza statystyki i terminy z bazy bez API. Przy pierwszym imporcie historii **nie** wysyła eventów `paragony_new_receipt`. Zapisuje zrotowany refresh token do `entry.data`.
- `sensor.py`: ostatni zakup (kwota i atrybuty z pozycjami), data ostatniego zakupu, wydatki w bieżącym miesiącu, liczba paragonów.
- `__init__.py`:
  - akcje `paragony.search` (`SupportsResponse.ONLY`; filtry: `product`, `date_from`/`date_to` w lokalnej strefie, `chain`, `include_deposits`, `limit`) i `paragony.sync`;
  - jedna wspólna instancja bazy w `hass.data[DOMAIN]["db"]`.
- Produkty cykliczne (od 0.2):
  - tabele `tracked_products` i `tracked_product_names` (nazwa na liście zakupów ↔ nazwy z paragonów; ostatni zakup liczony ze wszystkich sieci);
  - `restock.py` (`evaluate`, czysta logika: termin = ostatni zakup + `interval_days`; dodanie **raz na cykl**, czyli tylko gdy `last_added_at < last_purchased_at`);
  - `coordinator._async_restock` (`todo.get_items`, potem `todo.add_item`, `mark_added`, event `paragony_restock_added`), wołane po synchronizacji, co godzinę i po zmianie `number`;
  - `async_update_local` celowo nie używa `async_set_updated_data`, bo ta przesuwa termin synchronizacji;
  - `entity.py` (`RestockEntity`, urządzenie „Zakupy cykliczne”), `number.py` (dni) i `RestockSensor` (data);
  - produkty są w bazie, nie w `entry.options` (tam tylko `todo_entity`). `OptionsFlow` po zmianie robi `async_schedule_reload`, a przy usuwaniu kasuje encje z rejestru.
- Paragony ze zdjęć (od 0.4):
  - `photo.py` (czysta logika): `INSTRUCTIONS` dla modelu, `build_receipt(data, tz, source)` → `PhotoResult` (`receipt`, `items_total`, `mismatch`), `detect_chain` (aliasy w `const.PHOTO_CHAIN_ALIASES`, nieznane → `inne`), `external_id` = `photo-<hash(sieć, minuta, suma, nr paragonu)>`.
  - Odpowiedź modelu jest zwięzła: pozycje to tablice `[nazwa, ilość, wartość, rabat, "p"|"k"]`, bo pełne obiekty przekraczały limit tokenów. Pozycje jako obiekty też są akceptowane. Zweryfikowane na prawdziwym e-paragonie Biedronki (31 pozycji, suma co do grosza, Gemini 3 Flash, około 40 s). Gemini liczy „myślenie” do `max_tokens`, więc przy 1000 dostawał `MAX_TOKENS` (u mnie podniesione do 19400).
  - Akcja `paragony.add_from_image` (`SupportsResponse.OPTIONAL`) woła `ai_task.generate_data` z załącznikiem z media source. Celowo bez `structure`: model zwraca JSON w tekście, bo zagnieżdżona lista pozycji nie przechodzi przez selektory u wszystkich dostawców.
  - Duplikaty: `db.find_similar` (ta sama suma ±10 min, dowolna sieć), więc zdjęcie e-paragonu z Żabki/Lidla nie zdubluje wpisu.
  - Po zapisie: event `paragony_new_receipt` i `coordinator.async_update_local()` dla wszystkich wpisów (statystyki i terminy bez odpytywania API).
- `config_flow.py`: menu wyboru sieci (`user` → `phone` | `lidl`), reauth wraca do kroku właściwej sieci.
  - Żabka: numer telefonu → kod SMS → `entry.data = {chain, phone, refresh_token}`, `unique_id` = `zabka_<numer>`.
  - Lidl: link PKCE → użytkownik wkleja adres `com.lidlplus.app://callback?code=…` → `entry.data = {chain, refresh_token}`, `unique_id` = `lidl_<sub z access tokenu>`. Po błędzie generowane jest nowe PKCE, bo kod jest jednorazowy.

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

## Nieoficjalne API Lidl Plus
Klient OAuth `LidlPlusNativeClient` (sekret `secret`, Basic auth), `redirect_uri` `com.lidlplus.app://callback`, scope `openid profile offline_access lpprofile lpapis`. Na podstawie bibliotek `lidl-plus` i `ilidl`.
1. `https://accounts.lidl.com/connect/authorize?…&code_challenge=…&Country=PL&language=pl-PL` to logowanie w przeglądarce (z 2FA). Bez Selenium: użytkownik kopiuje adres przekierowania z DevTools.
2. `POST /connect/token` (`authorization_code` + `code_verifier` albo `refresh_token`). Refresh token rotuje, a `400` (`invalid_grant`) oznacza reauth.
3. Nagłówki do API paragonów: `Authorization: Bearer`, `App-Version`, `Operating-System: iOs`, `App: com.lidl.eci.lidl.plus`, `Accept-Language: pl`.
4. `GET https://tickets.lidlplus.com/api/v2/PL/tickets?pageNumber=N&onlyFavorite=false` zwraca listę (`tickets`, `totalCount`, `size` = 25).
5. `GET https://tickets.lidlplus.com/api/v3/PL/tickets/<id>` zwraca szczegóły (`ticketType` `NATIVE` albo `HTML`).

## Dodawanie nowej sieci
1. Dodaj stałą sieci w `const.py` (`CHAIN_*`, `CHAIN_NAMES`).
2. Napisz `providers/<siec>.py` implementujący `ReceiptProvider`.
3. Dodaj krok w `config_flow.py` i gałąź w `async_setup_entry`.
4. Dopisz sieć do selektora `chain` w `services.yaml` oraz do tłumaczeń (`strings.json`, `translations/*.json`).

Tropy:
- **Biedronka (Moja Biedronka)**, rozpoznane 2026-10-02 na podstawie `Przemko92/home-assistant-mojabiedronka` i `aqbifzl/biedronka-cli`:
  - Logowanie: Keycloak `https://konto.biedronka.pl/realms/loyalty/protocol/openid-connect/{auth,token}`, publiczny klient `cma20` (bez sekretu, `client_id` w treści), PKCE, `redirect_uri` `app://cma20.biedronka.pl`. W przeglądarce: telefon → Cloudflare Turnstile → SMS. Kod jest ważny około minuty. Access token żyje 24 h, refresh token 120 dni i rotuje.
  - Cloudflare przed Keycloakiem odrzuca domyślny User-Agent (`403 error code: 1010`), więc trzeba wysyłać przeglądarkowy UA.
  - API `https://api.prod.biedronka.cloud/api/v7` (`User-Agent: Android/2.22.2`, `Bearer`):
    - `transactions/?page=N` zwraca `transactions`, `page_count`, a w transakcjach `id`, `date`, `total_price`, `store_name`, `receipt_num` i `is_e_receipt_available`;
    - `transactions/<id>/e-receipt/` z nagłówkiem `output-format: json` zwraca linie `sellLine`, `discountLine`, `discountSummary`, `vatSummary`, `sumInCurrency`. To nie JPK. Czasem daje 404, wtedy fallback na `transactions/<id>/` (`items`).
  - Stan: logowanie działa (`tools/biedronka_discover.py`), ale konto nie ma jeszcze transakcji, a `users/me` zwraca `electronic_carrier: null`. Formatu nie zweryfikowano na prawdziwych danych.

## Testy
```bash
python3 -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest
```
- `tests/conftest.py` ładuje moduły jako pakiet `paragony` bez wykonywania `__init__.py`, więc testy parsera i bazy działają bez HA.
- `tests/test_integration.py` uruchamia prawdziwy HA z podmienionym providerem. Katalog konfiguracji jest przestawiony na `tmp_path`, bo inaczej `paragony.db` zostaje w `testing_config` biblioteki.
- `tests/fixtures/zabka_receipt.json` i `lidl_receipts.json` są **syntetyczne**. Nie commituj prawdziwych paragonów ani numerów telefonów.

## Narzędzia
- `tools/zabka_discover.py`: ręczne rozpoznawanie API (`send`, `verify`, `introspect`, `probe`, `query`). Tokeny trafiają do `.secrets/` (gitignored, chmod 600).
- `tools/biedronka_discover.py`: `url`, `code <adres>`, `list`, `fetch [N]`. Wyniki trafiają do `.secrets/biedronka/`.
- `tools/lidl_discover.py`: `url` (link PKCE), `code <adres>`, `list`, `fetch [N]`. Paragony trafiają do `.secrets/lidl/`.

## Konwencje
- Komunikaty, docstringi i komentarze po polsku; dopasuj się do stylu istniejącego kodu.
- Bez zewnętrznych zależności w `manifest.json` (aiohttp pochodzi z HA).
- Nie loguj tokenów.
