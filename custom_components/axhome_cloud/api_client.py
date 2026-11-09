"""AX HOME's private Hik-Connect client lifecycle."""
from __future__ import annotations

import asyncio
import aiohttp

from homeassistant.exceptions import HomeAssistantError

from .hikconnect.exceptions import LoginError


async def async_get_api(hass, device_serial: str | None = None):
    """Return AX HOME's own authenticated client, refreshing it when needed."""
    domain_data = hass.data.get("axhome_cloud", {})
    accounts = domain_data.get("accounts", {})
    account = next(
        (item for item in accounts.values()
         if device_serial is None or item.get("device_serial") == device_serial),
        None,
    )
    if account is None and device_serial is None and len(accounts) == 1:
        account = next(iter(accounts.values()))
    if account is None:
        raise HomeAssistantError(
            "No hay una cuenta AX HOME Cloud configurada para este panel. "
            "Añade AX HOME Cloud desde Dispositivos y servicios."
        )
    api = account["api"]
    lock = account["refresh_lock"]
    async with lock:
        try:
            if api.is_refresh_login_needed():
                await api.refresh_login()
        except (LoginError, aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            raise HomeAssistantError(
                "La sesión de Hik-Connect no pudo renovarse. Reautentica AX HOME Cloud."
            ) from exc
    return api
