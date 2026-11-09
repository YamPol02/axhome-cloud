"""AX HOME cloud integration and read-only diagnostics."""
import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import quote
import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from .api_client import async_get_api
from .hikconnect.api import HikConnect
from .hikconnect.exceptions import LoginError
from .security import PinSecurity

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.ALARM_CONTROL_PANEL, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema("axhome_cloud")

async def async_setup(hass, config):
    domain_data = hass.data.setdefault("axhome_cloud", {})
    domain_data["yaml_config"] = config
    domain_data.setdefault("accounts", {})
    security = PinSecurity(hass)
    await security.load()
    hass.data["axhome_cloud"]["security"] = security
    card_path = Path(__file__).parent / "www" / "axhome-cards.js"
    if card_path.is_file() and not domain_data.get("frontend_registered"):
        url = "/axhome_cloud/axhome-cards.js"
        await hass.http.async_register_static_paths([
            StaticPathConfig(url, str(card_path), cache_headers=False)
        ])
        frontend.add_extra_js_url(hass, url)
        domain_data["frontend_registered"] = True
    zone_mode_store = Store(hass, 1, "axhome_cloud.zone_modes")
    zone_mode_data = await zone_mode_store.async_load() or {}

    def panel_zones(entity_id):
        state = hass.states.get(entity_id)
        if state is None or state.attributes.get("axhome_cloud") is not True:
            raise HomeAssistantError("Entidad de alarma AX HOME no válida.")
        serial = state.attributes.get("device_serial")
        area_id = state.attributes.get("area_id")
        if not serial or area_id is None:
            raise HomeAssistantError("La entidad de alarma no tiene identificador del panel y área.")
        zones = []
        for entity in hass.states.async_all("sensor"):
            attrs = entity.attributes
            if attrs.get("axhome_sensor") is not True or attrs.get("device_serial") != serial:
                continue
            try:
                zone_id = int(attrs["zone_id"])
            except (KeyError, TypeError, ValueError):
                continue
            zones.append({"zone_id": zone_id,
                          "name": str(attrs.get("zone_name") or entity.name or entity.entity_id)})
        zones.sort(key=lambda zone: zone["zone_id"])
        return f"{serial}:{area_id}", zones

    @websocket_api.websocket_command({vol.Required("type"): "axhome_cloud/get_zone_modes",
                                      vol.Required("entity_id"): str})
    @websocket_api.async_response
    async def get_zone_modes(hass, connection, msg):
        try:
            key, zones = panel_zones(msg["entity_id"])
        except HomeAssistantError as exc:
            connection.send_error(msg["id"], "zone_mode_error", str(exc))
            return
        saved = zone_mode_data.get(key)
        ids = [zone["zone_id"] for zone in zones]
        if saved is None:
            home = away = ids
            configured = False
        else:
            home = [zone_id for zone_id in saved.get("home", []) if zone_id in ids]
            away = [zone_id for zone_id in saved.get("away", []) if zone_id in ids]
            # Newly discovered zones remain protected in both modes until configured.
            new_zones = [zone_id for zone_id in ids if zone_id not in saved.get("known_zones", [])]
            home = sorted(set(home + new_zones))
            away = sorted(set(away + new_zones))
            configured = True
        connection.send_result(msg["id"], {"zones": zones, "home": home, "away": away,
                                           "configured": configured})

    @websocket_api.websocket_command({vol.Required("type"): "axhome_cloud/set_zone_modes",
                                      vol.Required("entity_id"): str,
                                      vol.Required("current_code"): str,
                                      vol.Required("home"): [vol.All(vol.Coerce(int), vol.Range(min=1, max=32))],
                                      vol.Required("away"): [vol.All(vol.Coerce(int), vol.Range(min=1, max=32))]})
    @websocket_api.async_response
    async def set_zone_modes(hass, connection, msg):
        if connection.user is None or not connection.user.is_admin:
            connection.send_error(msg["id"], "unauthorized", "Solo un administrador puede cambiar la asignación de zonas.")
            return
        try:
            key, zones = panel_zones(msg["entity_id"])
        except HomeAssistantError as exc:
            connection.send_error(msg["id"], "zone_mode_error", str(exc))
            return
        try:
            await security.verify(msg["current_code"])
        except HomeAssistantError as exc:
            connection.send_error(msg["id"], "pin_error", str(exc))
            return
        known = {zone["zone_id"] for zone in zones}
        home, away = set(msg["home"]), set(msg["away"])
        if home - known or away - known:
            connection.send_error(msg["id"], "invalid_zone", "La lista contiene sensores que no pertenecen a este panel.")
            return
        zone_mode_data[key] = {"home": sorted(home), "away": sorted(away), "known_zones": sorted(known)}
        await zone_mode_store.async_save(zone_mode_data)
        connection.send_result(msg["id"], {"zones": zones, "home": sorted(home),
                                           "away": sorted(away), "configured": True})

    websocket_api.async_register_command(hass, get_zone_modes)
    websocket_api.async_register_command(hass, set_zone_modes)

    @websocket_api.websocket_command({vol.Required("type"): "axhome_cloud/pin_status"})
    @websocket_api.async_response
    async def pin_status(hass, connection, msg):
        connection.send_result(msg["id"], security.status())

    @websocket_api.websocket_command({vol.Required("type"): "axhome_cloud/set_pin",
                                      vol.Required("new_code"): str,
                                      vol.Optional("current_code"): str})
    @websocket_api.async_response
    async def set_pin(hass, connection, msg):
        if connection.user is None or not connection.user.is_admin:
            connection.send_error(msg["id"], "unauthorized", "Solo un administrador puede configurar el PIN.")
            return
        try:
            await security.change(msg["new_code"], msg.get("current_code"))
        except HomeAssistantError as exc:
            connection.send_error(msg["id"], "pin_error", str(exc))
            return
        connection.send_result(msg["id"], security.status())

    websocket_api.async_register_command(hass, pin_status)
    websocket_api.async_register_command(hass, set_pin)
    @websocket_api.websocket_command({
        vol.Required("type"): "axhome_cloud/siren_sound",
        vol.Required("entity_id"): str,
    })
    @websocket_api.async_response
    async def siren_sound(hass, connection, msg):
        if connection.user is None or not connection.user.is_admin:
            connection.send_error(msg["id"], "unauthorized", "Solo administradores.")
            return
        state = hass.states.get(msg["entity_id"])
        alarms = hass.data["axhome_cloud"].get("siren_alarms", {})
        alarm = alarms.get(msg["entity_id"])
        try:
            api = await async_get_api(hass, getattr(alarm, "serial", None))
        except HomeAssistantError:
            api = None
        if state is None or alarm is None or api is None:
            connection.send_error(msg["id"], "unavailable", "Panel AX HOME no disponible.")
            return
        base = api.BASE_URL.rstrip("/") + "/v3/iot-feature/"
        path = quote(alarm.serial, safe="") + "/global/0/SirenMgr/"
        result = {}
        for key, method, route, payload in (
            ("sirens", "PUT", "action/" + path + "SearchSirenStatus",
             {"searchID": "HA01", "searchResultPosition": 1, "maxResults": 4}),
            ("volume", "GET", "feature/" + path + "SirenLinkageParam", None),
        ):
            try:
                async with asyncio.timeout(15):
                    options = {"allow_redirects": False, "raise_for_status": False}
                    if payload is not None:
                        options["json"] = payload
                    async with api.client.request(method, base + route, **options) as response:
                        body = json.loads(await response.text())
                        meta = body.get("meta", {})
                        data = body.get("data")
                        if isinstance(data, str):
                            data = json.loads(data)
                        result[key] = {"http_status": response.status,
                                       "code": meta.get("code"),
                                       "message": meta.get("message"), "data": data}
            except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as exc:
                result[key] = {"error": type(exc).__name__}
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, siren_sound)

    async def diagnose(call):
        serial = call.data["device_serial"]
        api = await async_get_api(hass, serial)
        result = {"version": "0.4.1", "queries": {}}
        requests = [
            ("AlarmSubSystemMgr", "SearchSubSystemArmStatus", {"operationMode": "all"}),
            ("ZoneMgr", "SearchZoneStatus", {"searchID": "ha_zones", "searchResultPosition": 1, "maxResults": 32}),
            ("ZoneMgr", "SearchZoneBasicInfo", {"searchID": "ha_zones", "searchResultPosition": 1, "maxResults": 32}),
        ]
        for domain, action, payload in requests:
            url = api.BASE_URL.rstrip("/") + "/v3/iot-feature/action/" + quote(serial, safe="") + "/global/0/" + domain + "/" + action
            item = {"request": payload}
            try:
                async with asyncio.timeout(12):
                    async with api.client.put(url, json=payload, raise_for_status=False, allow_redirects=False) as response:
                        item["http_status"] = response.status
                        item["content_type"] = response.content_type
                        text = await response.text()
                        try:
                            body = json.loads(text)
                        except ValueError:
                            body = None
                        if isinstance(body, dict):
                            meta = body.get("meta")
                            if isinstance(meta, dict):
                                item["meta"] = {k: meta[k] for k in ("code", "message", "moreInfo") if k in meta}
                            item["data"] = body.get("data")
                            for key in ("code", "message", "error", "status", "detail", "errors"):
                                if key in body:
                                    item[key] = body[key]
                        else:
                            item["non_json_response"] = True
            except (aiohttp.ClientError, TimeoutError) as exc:
                item["transport_error"] = type(exc).__name__
            result["queries"][action] = item
        result["sensor_entities"] = [s.entity_id for s in hass.states.async_all("sensor") if s.attributes.get("axhome_sensor") is True]
        result["alarm_diagnostics"] = [{"entity_id":s.entity_id, "last_command_error":s.attributes.get("last_command_error")} for s in hass.states.async_all("alarm_control_panel") if s.attributes.get("axhome_cloud") is True]
        return result
    hass.services.async_register("axhome_cloud", "diagnose", diagnose,
        schema=vol.Schema({vol.Required("device_serial"):vol.All(str,vol.Match(r"^[A-Za-z0-9-]{5,64}$"))}),
        supports_response=SupportsResponse.ONLY)
    return True


async def async_setup_entry(hass, entry: config_entries.ConfigEntry):
    """Create an independent Hik-Connect session owned by AX HOME Cloud."""
    api = HikConnect()
    try:
        async with asyncio.timeout(30):
            await api.login(entry.data["username"], entry.data["password"])
    except LoginError as exc:
        await api.close()
        raise ConfigEntryAuthFailed("Credenciales Hik-Connect inválidas o sesión bloqueada.") from exc
    except (aiohttp.ClientError, TimeoutError, KeyError) as exc:
        await api.close()
        raise ConfigEntryNotReady("No se pudo iniciar sesión en Hik-Connect.") from exc

    domain_data = hass.data.setdefault("axhome_cloud", {})
    domain_data.setdefault("accounts", {})[entry.entry_id] = {
        "api": api,
        "device_serial": entry.data["device_serial"],
        "refresh_lock": asyncio.Lock(),
    }
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        domain_data["accounts"].pop(entry.entry_id, None)
        await api.close()
        raise
    return True


async def async_unload_entry(hass, entry: config_entries.ConfigEntry):
    """Unload entities and close the account session cleanly."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    account = hass.data.get("axhome_cloud", {}).get("accounts", {}).pop(entry.entry_id, None)
    if account:
        await account["api"].close()
    return True


