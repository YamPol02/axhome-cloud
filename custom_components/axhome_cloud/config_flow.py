"""Config flow for AX HOME Cloud."""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import voluptuous as vol
from homeassistant import config_entries

from .hikconnect.api import HikConnect
from .hikconnect.exceptions import LoginError

_LOGGER = logging.getLogger(__name__)
DOMAIN = "axhome_cloud"
BRAND_ICON = (
    "https://raw.githubusercontent.com/YamPol02/axhome-cloud/main/"
    "custom_components/axhome_cloud/brand/icon.png"
)


class AXHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Validate Hik-Connect credentials, then let the user select an AX panel."""

    VERSION = 1

    def __init__(self):
        self._credentials = None
        self._devices = {}
        self._reauth_entry = None

    async def async_step_reauth(self, entry_data):
        """Ask for a fresh cloud password after an expired/revoked login."""
        self.context["title_placeholders"] = {
            "name": "Datos de tu cuenta de Hik-Connect"
        }
        entry_id = self.context.get("entry_id")
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        if self._reauth_entry is None:
            return self.async_abort(reason="setup_error")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None and self._reauth_entry is not None:
            api = HikConnect()
            try:
                async with asyncio.timeout(25):
                    await api.login(
                        self._reauth_entry.data["username"],
                        user_input["password"],
                    )
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        "password": user_input["password"],
                    },
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            except LoginError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
                errors["base"] = "cannot_connect"
            finally:
                await api.close()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
            description_placeholders={
                "username": self._reauth_entry.data["username"],
                "brand_icon": BRAND_ICON,
            },
        )

    async def async_step_user(self, user_input=None):
        self.context["title_placeholders"] = {
            "name": "Datos de tu cuenta de Hik-Connect"
        }
        errors = {}
        if user_input is not None:
            api = HikConnect()
            try:
                async with asyncio.timeout(25):
                    username = user_input["username"].strip()
                    password = user_input["password"]
                    await api.login(username, password)
                    self._devices = {
                        device["serial"]: device async for device in api.get_devices()
                    }
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    self._credentials = {
                        "username": username,
                        "password": password,
                    }
                    return await self.async_step_device()
            except LoginError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
                _LOGGER.exception("AX HOME Cloud account validation failed")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error while setting up AX HOME Cloud")
                errors["base"] = "unknown"
            finally:
                await api.close()

        schema = vol.Schema({
            vol.Required("username"): str,
            vol.Required("password"): str,
        })
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"brand_icon": BRAND_ICON},
        )

    async def async_step_device(self, user_input=None):
        self.context["title_placeholders"] = {
            "name": "Selecciona el panel y el área"
        }
        if not self._devices or not self._credentials:
            return self.async_abort(reason="setup_error")
        if user_input is not None:
            serial = user_input["device_serial"]
            device = self._devices[serial]
            await self.async_set_unique_id(f"{self._credentials['username'].casefold()}:{serial}")
            self._abort_if_unique_id_configured()
            data = {
                **self._credentials,
                "device_serial": serial,
                "area_id": user_input["area_id"],
                "name": device.get("name") or "AX HOME",
                "cloud_user_type": user_input["cloud_user_type"],
            }
            return self.async_create_entry(title=f"AX HOME · {data['name']}", data=data)

        choices = {
            serial: f"{device.get('name', 'Dispositivo')} · {device.get('type', 'Hikvision')} · {serial}"
            for serial, device in self._devices.items()
        }
        schema = vol.Schema({
            vol.Required("device_serial"): vol.In(choices),
            vol.Required("area_id", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
            vol.Required("cloud_user_type", default="administrator"): vol.In(
                {"administrator": "Propietario", "operator": "Usuario compartido"}
            ),
        })
        return self.async_show_form(step_id="device", data_schema=schema)



