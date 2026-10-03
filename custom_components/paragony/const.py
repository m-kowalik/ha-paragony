"""Stałe integracji Paragony."""
from datetime import timedelta

DOMAIN = "paragony"

CONF_CHAIN = "chain"
CONF_PHONE = "phone"
CONF_CODE = "code"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CALLBACK_URL = "callback_url"

CHAIN_ZABKA = "zabka"
CHAIN_LIDL = "lidl"
CHAIN_OTHER = "inne"
CHAIN_NAMES = {
    CHAIN_ZABKA: "Żabka",
    CHAIN_LIDL: "Lidl",
    # sieci rozpoznawane tylko na zdjęciach paragonów
    "biedronka": "Biedronka",
    "kaufland": "Kaufland",
    "auchan": "Auchan",
    "carrefour": "Carrefour",
    "dino": "Dino",
    "netto": "Netto",
    "aldi": "Aldi",
    "stokrotka": "Stokrotka",
    "lewiatan": "Lewiatan",
    "polomarket": "POLOmarket",
    "intermarche": "Intermarché",
    "rossmann": "Rossmann",
    "hebe": "Hebe",
    "pepco": "Pepco",
    "action": "Action",
    "ikea": "IKEA",
    "leroy_merlin": "Leroy Merlin",
    "castorama": "Castorama",
    "obi": "OBI",
    CHAIN_OTHER: "Inne",
}
# fragmenty nazwy sprzedawcy (małe litery, bez polskich znaków) → sieć; także nazwy spółek z nagłówka paragonu
PHOTO_CHAIN_ALIASES: dict[str, tuple[str, ...]] = {
    CHAIN_ZABKA: ("zabka",),
    CHAIN_LIDL: ("lidl",),
    "biedronka": ("biedronka", "jeronimo martins"),
    "kaufland": ("kaufland",),
    "auchan": ("auchan",),
    "carrefour": ("carrefour",),
    "dino": ("dino polska", "dino"),
    "netto": ("netto",),
    "aldi": ("aldi",),
    "stokrotka": ("stokrotka",),
    "lewiatan": ("lewiatan",),
    "polomarket": ("polomarket", "polo market"),
    "intermarche": ("intermarche",),
    "rossmann": ("rossmann",),
    "hebe": ("hebe",),
    "pepco": ("pepco",),
    "action": ("action",),
    "ikea": ("ikea",),
    "leroy_merlin": ("leroy merlin",),
    "castorama": ("castorama",),
    "obi": ("obi",),
}

DB_FILENAME = "paragony.db"
UPDATE_INTERVAL = timedelta(hours=6)

EVENT_NEW_RECEIPT = "paragony_new_receipt"
EVENT_RESTOCK_ADDED = "paragony_restock_added"

CONF_TODO_ENTITY = "todo_entity"
RESTOCK_INTERVAL = timedelta(hours=1)
RECENT_DAYS = 30
DEFAULT_INTERVAL_DAYS = 7

SERVICE_SEARCH = "search"
SERVICE_SYNC = "sync"
SERVICE_ADD_FROM_IMAGE = "add_from_image"
