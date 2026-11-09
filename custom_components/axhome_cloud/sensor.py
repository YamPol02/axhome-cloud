"""Read-only zone monitoring through the AX HOME OTAP profile."""
import asyncio
import json
import logging
from datetime import timedelta
from urllib.parse import quote

import aiohttp
import voluptuous as vol
from homeassistant.components.sensor import SensorEntity, PLATFORM_SCHEMA
from homeassistant.exceptions import HomeAssistantError, PlatformNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from .api_client import async_get_api

_LOGGER = logging.getLogger(__name__)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required("device_serial"): vol.All(cv.string, vol.Match(r"^[A-Za-z0-9-]{5,64}$")),
    vol.Optional("poll_interval", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    options = discovery_info if discovery_info is not None else config
    coordinator = ZoneCoordinator(hass, options["device_serial"], options.get("poll_interval", 1))
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise PlatformNotReady("No se pudo consultar SearchZoneStatus. Revisa los registros de AX HOME.")
    known = set()
    def discover():
        if not coordinator.last_update_success or coordinator.data is None:
            return
        new = set(coordinator.data) - known
        if new:
            known.update(new)
            async_add_entities([AXHomeZone(coordinator, zone) for zone in sorted(new)])
    # Retain discovery listener for zones added later; HA owns this platform until restart.
    coordinator.async_add_listener(discover)
    discover()


async def async_setup_entry(hass, entry, async_add_entities):
    """Create and continuously discover sensor entities from a config entry."""
    serial = entry.data["device_serial"]
    interval = entry.options.get("poll_interval", 1)
    coordinator = ZoneCoordinator(hass, serial, interval)
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise PlatformNotReady("No se pudo consultar el estado de las zonas AX HOME.")
    known = set()

    def discover():
        if not coordinator.last_update_success or coordinator.data is None:
            return
        new = set(coordinator.data) - known
        if new:
            known.update(new)
            async_add_entities([AXHomeZone(coordinator, zone) for zone in sorted(new)])

    coordinator.async_add_listener(discover)
    discover()

class ZoneCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, serial, interval):
        super().__init__(hass, _LOGGER, name="AX HOME sensores", update_interval=timedelta(seconds=interval))
        self.serial = serial
        self.basic = {}
        self.basic_read_at = None
        self.metadata_error = None

    async def request(self, action, payload):
        api = await async_get_api(self.hass, self.serial)
        url = api.BASE_URL.rstrip("/") + "/v3/iot-feature/action/" + quote(self.serial, safe="") + "/global/0/ZoneMgr/" + action
        try:
            async with asyncio.timeout(25):
                async with api.client.put(url, json=payload, raise_for_status=False, allow_redirects=False) as response:
                    http_status = response.status
                    try:
                        body = json.loads(await response.text())
                    except ValueError:
                        raise HomeAssistantError(f"{action}: HTTP {http_status}, respuesta no JSON") from None
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise HomeAssistantError(f"No se pudo consultar {action}") from exc
        meta = body.get("meta") if isinstance(body, dict) else None
        if http_status != 200 or not isinstance(meta, dict) or str(meta.get("code")) != "200":
            msg = str(meta.get("message", ""))[:500] if isinstance(meta, dict) else "Respuesta inesperada"
            detail = str(meta.get("moreInfo", ""))[:800] if isinstance(meta, dict) else ""
            raise HomeAssistantError(f"{action}: HTTP {http_status}: {msg}; detalle={detail}")
        data = body.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as exc:
                raise HomeAssistantError(f"{action}: datos no JSON") from exc
        return data

    async def search(self, action):
        rows = {}
        position = 1
        while position <= 96:
            data = await self.request(action, {
                "searchID": "ha_zones", "searchResultPosition": position, "maxResults": 32,
            })
            if not isinstance(data, dict) or not isinstance(data.get("matchResults", []), list):
                raise HomeAssistantError(f"{action}: formato de zonas inesperado")
            response_status = data.get("responseStatus")
            if response_status not in ("OK", "MORE", "NO MATCH"):
                raise HomeAssistantError(f"{action}: responseStatus={response_status}")
            batch = data.get("matchResults", [])
            for row in batch:
                if not isinstance(row, dict) or isinstance(row.get("zoneID"), bool):
                    continue
                try:
                    zone = int(row["zoneID"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 1 <= zone <= 32:
                    rows[zone] = row
            if response_status != "MORE":
                return rows
            if not batch:
                raise HomeAssistantError(f"{action}: paginación incompleta")
            position += len(batch)
        raise HomeAssistantError(f"{action}: demasiadas páginas")

    async def _async_update_data(self):
        try:
            statuses = await self.search("SearchZoneStatus")
            now = dt_util.utcnow()
            if self.basic_read_at is None or now - self.basic_read_at >= timedelta(minutes=15):
                try:
                    self.basic = await self.search("SearchZoneBasicInfo")
                    self.metadata_error = None
                except HomeAssistantError as exc:
                    self.metadata_error = str(exc)
                self.basic_read_at = now
            return {zone: {"status": row, "info": self.basic.get(zone, {}),
                           "observed_at": now.isoformat()} for zone, row in statuses.items()}
        except HomeAssistantError as exc:
            raise UpdateFailed(str(exc)) from exc

class AXHomeZone(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:shield-home-outline"
    def __init__(self, coordinator, zone):
        super().__init__(coordinator)
        self.zone = zone
        self._attr_unique_id = f"axhome_cloud_{coordinator.serial}_zone_{zone}"
        self._attr_name = f"AX HOME Zona {zone:02d}"

    @property
    def available(self):
        return super().available and self.zone in (self.coordinator.data or {})

    @property
    def native_value(self):
        row = (self.coordinator.data or {}).get(self.zone, {}).get("status", {})
        if row.get("onlineStatus") == "offline":
            return "offline"
        if row.get("alarmStatus") == "alarm":
            return "alarm"
        if row.get("antiDismantle") == "abnormal":
            return "tamper"
        if row.get("faultStatus") == "fault":
            return "fault"
        if row.get("batteryStatus") == "lowPower":
            return "low_battery"
        if row.get("triggerStatus") == "triggered":
            return "triggered"
        # 'online' indicates connectivity, never 'all sensors healthy'.
        return row.get("onlineStatus") if row.get("onlineStatus") == "online" else None

    @property
    def extra_state_attributes(self):
        data = (self.coordinator.data or {}).get(self.zone, {})
        return {"axhome_sensor": True, "device_serial": self.coordinator.serial, "zone_id": self.zone,
                "zone_name": data.get("info", {}).get("zoneName", f"Zona {self.zone}"),
                "observed_at": data.get("observed_at"),
                "status": data.get("status", {}), "info": data.get("info", {}),
                "metadata_error": self.coordinator.metadata_error}
