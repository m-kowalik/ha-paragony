"""Kreator konfiguracji: wybór sieci → numer telefonu → kod SMS."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CHAIN_NAMES, CHAIN_ZABKA, CONF_CHAIN, CONF_CODE, CONF_PHONE, CONF_REFRESH_TOKEN, DOMAIN
from .providers.base import ProviderAuthError, ProviderError
from .providers.zabka import ZabkaProvider, normalize_phone

_LOGGER = logging.getLogger(__name__)


class ParagonyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._provider: ZabkaProvider | None = None
        self._phone: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # na razie obsługiwana jest tylko Żabka; kolejne sieci dostaną własny krok
        return await self.async_step_phone()

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

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._phone = entry_data.get(CONF_PHONE)
        return await self.async_step_phone()
