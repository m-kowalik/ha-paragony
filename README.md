# Paragony — e-paragony w Home Assistant

Custom integration pobierająca e-paragony z aplikacji sieci handlowych i zapisująca
każdą pozycję (produkt, ilość, cena po rabacie, data, sklep) w lokalnej bazie SQLite
`/config/paragony.db`.

Obsługiwane sieci: **Żabka (Żappka)**. W planach: Lidl Plus, Biedronka.

> Integracja korzysta z nieoficjalnego API aplikacji Żappka — może przestać działać po zmianach po stronie Żabki.

## Instalacja (HACS)
1. HACS → ⋮ → *Custom repositories* → URL tego repozytorium, typ *Integration*.
2. Zainstaluj „Paragony” i zrestartuj Home Assistant.
3. Ustawienia → Urządzenia i usługi → Dodaj integrację → **Paragony** → numer telefonu → kod SMS.

Przy pierwszej synchronizacji importowana jest cała dostępna historia, potem co 6 h tylko nowe paragony.

## Encje
| Encja | Opis |
|---|---|
| `sensor.zabka_ostatni_zakup` | kwota ostatniego paragonu; atrybuty: data, sklep, adres, lista pozycji |
| `sensor.zabka_data_ostatniego_zakupu` | znacznik czasu ostatniego zakupu |
| `sensor.zabka_wydatki_w_tym_miesiacu` | suma paragonów od 1. dnia miesiąca (atrybut `receipts`) |
| `sensor.zabka_liczba_paragonow` | liczba paragonów w bazie |
| `sensor.zabka_ostatnio_kupione_30_dni` | liczba różnych produktów z 30 dni; atrybut `products` (nazwa, ostatni zakup, ile razy) |

## Produkty kupowane cyklicznie → lista zakupów (np. Bring)
Ustawienia → Paragony → **Konfiguruj**:
1. **Lista zakupów** — wybierz encję `todo` (np. listę Bring).
2. **Dodaj produkt kupowany cyklicznie** — zaznacz nazwy z paragonów oznaczające ten sam produkt,
   podaj nazwę na liście zakupów i co ile dni go kupujesz (podpowiedź liczona z historii zakupów).

Dla każdego produktu powstają encje w urządzeniu „Zakupy cykliczne”:
- `number.zakupy_cykliczne_<produkt>_co_ile_dni` — edytowalne z dashboardu,
- `sensor.zakupy_cykliczne_<produkt>_kup_ponownie` — data (atrybuty: ostatni zakup, dni do zakupu, nazwy z paragonów, kiedy dodano do listy).

Gdy od ostatniego zakupu minie ustawiona liczba dni, produkt jest dodawany do listy (sprawdzane co godzinę
i po każdej synchronizacji) — **raz na cykl**: kolejne dodanie dopiero po nowym paragonie z tym produktem.
Jeśli produkt już czeka na liście, nie jest dublowany. Po dodaniu wysyłany jest event `paragony_restock_added`.

## Akcje
**`paragony.search`** (zwraca odpowiedź) — pozycje z paragonów, od najnowszych:
```yaml
action: paragony.search
data:
  product: cola        # fragment nazwy, bez rozróżniania wielkości liter
  date_from: 2026-09-01
  date_to: 2026-09-30
  # chain: zabka
  # include_deposits: true
  # limit: 100
response_variable: zakupy
```
Odpowiedź: `count`, `total` (PLN) i `items` z polami `purchased_at`, `chain`, `store`, `address`,
`product`, `quantity`, `unit`, `unit_price`, `price` (po rabacie), `discount`.

**`paragony.sync`** — natychmiastowa synchronizacja wszystkich kont.

## Event
`paragony_new_receipt` — po każdym nowym paragonie (poza pierwszym importem historii):
`chain`, `receipt_id`, `purchased_at`, `store`, `total`, `currency`, `items[]`.

## Baza danych
`receipts(chain, external_id, purchased_at UTC, store_name, store_address, total, currency, raw_json)`
i `items(receipt_id, position, name, raw_name, kind product|deposit, quantity, unit, unit_price, total_price, discount, final_price)`.
Kwoty w groszach. Można jej używać np. z integracją SQL.

## Rozwój
```bash
python3 -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest
```
`tools/zabka_discover.py` — skrypt do ręcznego rozpoznawania API (tokeny w `.secrets/`, poza gitem).
