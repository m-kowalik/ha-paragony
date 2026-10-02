"""Kreator konfiguracji: wybór sieci → Żabka: numer telefonu i kod SMS / Lidl: logowanie w przeglądarce."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import logging
import sqlite3
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .const import (
    CHAIN_LIDL,
    CHAIN_NAMES,
    CHAIN_ZABKA,
    CONF_CALLBACK_URL,
    CONF_CHAIN,
    CONF_CODE,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_TODO_ENTITY,
    DEFAULT_INTERVAL_DAYS,
    DOMAIN,
)
from .db import ReceiptDB
from .providers.base import ProviderAuthError, ProviderError
from .providers.lidl import LidlProvider, extract_code, generate_pkce, login_url
from .providers.zabka import ZabkaProvider, normalize_phone
from .restock import bring_name

CONF_ITEM_NAMES = "item_names"
CONF_NAME = "name"
CONF_INTERVAL = "interval_days"
CONF_PRODUCT = "product"
CONF_PRODUCTS = "products"

_LOGGER = logging.getLogger(__name__)


class ParagonyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ParagonyOptionsFlow:
        return ParagonyOptionsFlow()

    def __init__(self) -> None:
        self._provider: ZabkaProvider | None = None
        self._phone: str | None = None
        self._pkce: tuple[str, str] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["phone", "lidl"])

    async def async_step_phone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if len(phone) != 9:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                if self.source != "reauth":
                    await self.async_set_unique_id(f"{CHAIN_ZABKA}_{phone}")
                    self._abort_if_unique_id_configured()
                self._phone = phone
                self._provider = ZabkaProvider(async_get_clientsession(self.hass))
                try:
                    await self._provider.async_send_code(phone)
                except ProviderError:
                    _LOGGER.exception("Nie udało się wysłać kodu SMS")
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_code()

        default = self._phone or ""
        return self.async_show_form(
            step_id="phone",
            data_schema=vol.Schema({vol.Required(CONF_PHONE, default=default): str}),
            errors=errors,
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._provider is not None and self._phone is not None
            try:
                refresh_token = await self._provider.async_sign_in(self._phone, user_input[CONF_CODE])
            except ProviderAuthError:
                errors[CONF_CODE] = "invalid_code"
            except ProviderError:
                _LOGGER.exception("Logowanie do Żappki nie powiodło się")
                errors["base"] = "cannot_connect"
            else:
                data = {CONF_CHAIN: CHAIN_ZABKA, CONF_PHONE: self._phone, CONF_REFRESH_TOKEN: refresh_token}
                if self.source == "reauth":
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                return self.async_create_entry(
                    title=f"{CHAIN_NAMES[CHAIN_ZABKA]} ({self._phone[:3]} {self._phone[3:6]} {self._phone[6:]})",
                    data=data,
                )

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            description_placeholders={"phone": self._phone or ""},
            errors=errors,
        )

    async def async_step_lidl(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Lidl Plus: logowanie na stronie Lidla, potem wklejenie adresu z kodem autoryzacji."""
        errors: dict[str, str] = {}
        if self._pkce is None:
            self._pkce = generate_pkce()
        verifier, challenge = self._pkce
        if user_input is not None:
            provider = LidlProvider(async_get_clientsession(self.hass))
            try:
                refresh_token = await provider.async_exchange_code(
                    extract_code(user_input[CONF_CALLBACK_URL]), verifier
                )
            except ProviderAuthError:
                errors[CONF_CALLBACK_URL] = "invalid_auth_code"
            except ProviderError:
                _LOGGER.exception("Logowanie do Lidl Plus nie powiodło się")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{CHAIN_LIDL}_{provider.account_id}")
                data = {CONF_CHAIN: CHAIN_LIDL, CONF_REFRESH_TOKEN: refresh_token}
                if self.source == "reauth":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Lidl Plus", data=data)
            # kod autoryzacji jest jednorazowy — przy kolejnej próbie potrzebne nowe logowanie
            self._pkce = generate_pkce()
            challenge = self._pkce[1]

        return self.async_show_form(
            step_id="lidl",
            data_schema=vol.Schema({vol.Required(CONF_CALLBACK_URL): str}),
            description_placeholders={"url": login_url(challenge)},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        if entry_data.get(CONF_CHAIN) == CHAIN_LIDL:
            return await self.async_step_lidl()
        self._phone = entry_data.get(CONF_PHONE)
        return await self.async_step_phone()


def _interval_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=1, max=365, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="d")
    )


class ParagonyOptionsFlow(OptionsFlow):
    """Lista zakupów i produkty kupowane cyklicznie."""

    def __init__(self) -> None:
        self._item_names: list[str] = []
        self._product: dict | None = None

    @property
    def _db(self) -> ReceiptDB:
        return self.config_entry.runtime_data.db

    async def _run(self, func, *args, **kwargs):
        return await self.hass.async_add_executor_job(lambda: func(*args, **kwargs))

    async def _tracked(self) -> list[dict]:
        return await self._run(self._db.list_tracked, self.config_entry.entry_id)

    async def _item_options(self, extra: list[str] = ()) -> list[SelectOptionDict]:
        rows = await self._run(self._db.recent_products, 1000)
        options = []
        for row in rows:
            day = dt_util.as_local(datetime.fromisoformat(row["last_purchased_at"]))
            options.append(
                SelectOptionDict(value=row["name"], label=f"{row['name']} — ostatnio {day:%d.%m.%Y}, {row['times']}×")
            )
        known = {row["name"] for row in rows}
        options += [SelectOptionDict(value=name, label=name) for name in extra if name not in known]
        return options

    def _finish(self) -> ConfigFlowResult:
        # zmiany produktów są w bazie, nie w opcjach — przeładowanie tworzy/usuwa encje
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(data=dict(self.config_entry.options))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if getattr(self.config_entry, "runtime_data", None) is None:
            return self.async_abort(reason="not_loaded")
        menu = ["todo_list", "add_product"]
        if await self._tracked():
            menu += ["edit_product", "remove_products"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_todo_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        current = self.config_entry.options.get(CONF_TODO_ENTITY)
        return self.async_show_form(
            step_id="todo_list",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TODO_ENTITY, default=current or vol.UNDEFINED): EntitySelector(
                        EntitySelectorConfig(domain="todo")
                    )
                }
            ),
        )

    async def async_step_add_product(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_ITEM_NAMES):
                self._item_names = user_input[CONF_ITEM_NAMES]
                return await self.async_step_add_details()
            errors[CONF_ITEM_NAMES] = "no_items"
        return self.async_show_form(
            step_id="add_product",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ITEM_NAMES): SelectSelector(
                        SelectSelectorConfig(
                            options=await self._item_options(), multiple=True, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_add_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            try:
                await self._run(
                    self._db.add_tracked,
                    self.config_entry.entry_id,
                    name,
                    int(user_input[CONF_INTERVAL]),
                    self._item_names,
                )
            except sqlite3.IntegrityError:
                errors[CONF_NAME] = "name_exists"
            else:
                return self._finish()
        suggested = await self._run(self._db.suggest_interval, self._item_names)
        return self.async_show_form(
            step_id="add_details",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=bring_name(self._item_names[0])): str,
                    vol.Required(CONF_INTERVAL, default=suggested or DEFAULT_INTERVAL_DAYS): _interval_selector(),
                }
            ),
            description_placeholders={
                "items": ", ".join(self._item_names),
                "suggested": str(suggested) if suggested else "brak (za mało zakupów)",
            },
            errors=errors,
        )

    async def async_step_edit_product(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        products = await self._tracked()
        if user_input is not None:
            self._product = next(p for p in products if str(p["id"]) == user_input[CONF_PRODUCT])
            return await self.async_step_edit_details()
        return self.async_show_form(
            step_id="edit_product",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRODUCT): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=str(p["id"]), label=p["name"]) for p in products]
                        )
                    )
                }
            ),
        )

    async def async_step_edit_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._product is not None
        product = self._product
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_ITEM_NAMES):
                errors[CONF_ITEM_NAMES] = "no_items"
            else:
                try:
                    await self._run(
                        self._db.update_tracked,
                        product["id"],
                        name=user_input[CONF_NAME].strip(),
                        interval_days=int(user_input[CONF_INTERVAL]),
                        item_names=user_input[CONF_ITEM_NAMES],
                    )
                except sqlite3.IntegrityError:
                    errors[CONF_NAME] = "name_exists"
                else:
                    return self._finish()
        return self.async_show_form(
            step_id="edit_details",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=product["name"]): str,
                    vol.Required(CONF_INTERVAL, default=product["interval_days"]): _interval_selector(),
                    vol.Required(CONF_ITEM_NAMES, default=product["item_names"]): SelectSelector(
                        SelectSelectorConfig(
                            options=await self._item_options(product["item_names"]),
                            multiple=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={"product": product["name"]},
            errors=errors,
        )

    async def async_step_remove_products(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            ids = [int(pid) for pid in user_input.get(CONF_PRODUCTS, [])]
            await self._run(self._db.delete_tracked, ids)
            registry = er.async_get(self.hass)
            prefixes = tuple(f"{self.config_entry.entry_id}_product_{pid}_" for pid in ids)
            for entity in er.async_entries_for_config_entry(registry, self.config_entry.entry_id):
                if entity.unique_id.startswith(prefixes):
                    registry.async_remove(entity.entity_id)
            return self._finish()
        products = await self._tracked()
        return self.async_show_form(
            step_id="remove_products",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PRODUCTS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=str(p["id"]), label=p["name"]) for p in products],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )
