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

SERVICE_SEARCH = "search"
SERVICE_SYNC = "sync"
