"""Stałe integracji Paragony."""
from datetime import timedelta

DOMAIN = "paragony"

CONF_CHAIN = "chain"
CONF_PHONE = "phone"
CONF_CODE = "code"
CONF_REFRESH_TOKEN = "refresh_token"

CHAIN_ZABKA = "zabka"
CHAIN_NAMES = {CHAIN_ZABKA: "Żabka"}

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
