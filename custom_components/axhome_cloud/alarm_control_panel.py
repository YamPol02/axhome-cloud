"""Experimental AX HOME alarm entity; commands never set optimistic state."""
import asyncio
import json
import logging
from time import monotonic
from datetime import timedelta
from urllib.parse import quote

import aiohttp
import voluptuous as vol
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity, AlarmControlPanelEntityFeature, PLATFORM_SCHEMA,
    CodeFormat,
)
from homeassistant.components.alarm_control_panel.const import AlarmControlPanelState
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_platform, discovery
from .api_client import async_get_api

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required("device_serial"): vol.All(cv.string, vol.Match(r"^[A-Za-z0-9-]{5,64}$")),
    vol.Optional("area_id", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
    vol.Optional("name", default="AX HOME Area 1"): cv.string,
    vol.Optional("cloud_user_type"): vol.In(["administrator", "operator"]),
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("clear_alarm", {vol.Required("code"): cv.string}, "async_clear_alarm")
    async_add_entities([AXHomeAlarm(hass, config)], True)
    full_config = hass.data.get("axhome_cloud", {}).get("yaml_config", {})
    manual = full_config.get("sensor", [])
    if isinstance(manual, dict):
        manual = [manual]
    configured = any(isinstance(item, dict) and item.get("platform") == "axhome_cloud"
                     and item.get("device_serial") == config["device_serial"] for item in manual)
    if not configured:
        hass.async_create_task(discovery.async_load_platform(
            hass, "sensor", "axhome_cloud",
            {"device_serial": config["device_serial"], "poll_interval": 1}, full_config))


async def async_setup_entry(hass, entry, async_add_entities):
    """Create an alarm panel directly from the independent AX HOME entry."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "clear_alarm", {vol.Required("code"): cv.string}, "async_clear_alarm"
    )
    config = {
        "device_serial": entry.data["device_serial"],
        "area_id": entry.data.get("area_id", 1),
        "name": entry.data.get("name", "AX HOME"),
        "cloud_user_type": entry.data.get("cloud_user_type", "administrator"),
    }
    async_add_entities([AXHomeAlarm(hass, config)], True)

class AXHomeAlarm(AlarmControlPanelEntity):
    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_HOME | AlarmControlPanelEntityFeature.ARM_AWAY
    _attr_code_arm_required = True
    _attr_code_format = CodeFormat.NUMBER

    def __init__(self, hass, config):
        self.hass = hass
        self.serial = config["device_serial"]
        self.area = config["area_id"]
        self.cloud_user_type = config.get("cloud_user_type")
        self._attr_name = config["name"]
        self._attr_unique_id = f"axhome_cloud_{self.serial}_{self.area}"
        self._attr_available = False
        self._attr_alarm_state = None
        self._attr_extra_state_attributes = {}
        self._last_error = None
        self._lock = asyncio.Lock()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        alarms = self.hass.data["axhome_cloud"].setdefault("siren_alarms", {})
        alarms[self.entity_id] = self
        self.async_on_remove(lambda: alarms.pop(self.entity_id, None))

    async def _control_headers(self, api):
        if self.cloud_user_type is None:
            raise HomeAssistantError("Configura cloud_user_type según tu relación con el panel: administrator para propietario; operator para usuario compartido.")
        cached = getattr(self, "_control_identity", None)
        if cached and cached[0] is api and cached[1] > monotonic():
            return cached[2]
        try:
            async with asyncio.timeout(15):
                async with api.client.get(api.BASE_URL.rstrip("/") + "/v3/users/info",
                                          raise_for_status=False, allow_redirects=False) as response:
                    if response.status != 200:
                        raise HomeAssistantError(f"No se pudo consultar la identidad de la cuenta: HTTP {response.status}")
                    body = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise HomeAssistantError("No se pudo consultar la identidad de Hik-Connect.") from exc
        info = body.get("userInfo") if isinstance(body, dict) else None
        username = info.get("username") if isinstance(info, dict) else None
        if not isinstance(username, str) or not username.strip():
            raise HomeAssistantError("Hik-Connect no devolvió userInfo.username; no se ha enviado la orden.")
        headers = {"additionInfo": json.dumps({"ENAuthentication": {
            "userName": username, "userType": self.cloud_user_type,
        }}, ensure_ascii=True, separators=(",", ":"))}
        self._control_identity = (api, monotonic() + 300, headers)
        return headers

    async def _request(self, action, payload):
        api = await async_get_api(self.hass, self.serial)
        url = (api.BASE_URL.rstrip("/") + "/v3/iot-feature/action/"
               + quote(self.serial, safe="") + "/global/0/AlarmSubSystemMgr/" + action)
        headers = await self._control_headers(api) if action.startswith("CtrlSubSystem") else {}
        try:
            async with asyncio.timeout(25):
                async with api.client.put(url, json=payload, headers=headers, raise_for_status=False, allow_redirects=False) as response:
                    http_status = response.status
                    try:
                        result = json.loads(await response.text())
                    except ValueError:
                        raise HomeAssistantError(f"{action}: HTTP {http_status}, respuesta no JSON") from None
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise HomeAssistantError("No se pudo confirmar la respuesta de Hik-Connect; consulta el estado antes de repetir la orden.") from exc
        if not isinstance(result, dict) or not isinstance(result.get("meta"), dict):
            raise HomeAssistantError(f"{action}: HTTP {http_status}, respuesta inesperada: {str(result)[:600]}")
        meta = result["meta"]
        if http_status != 200 or str(meta.get("code")) != "200":
            raise HomeAssistantError(f"{action}: HTTP {http_status}, código {meta.get('code')}: {str(meta.get('message', ''))[:500]}; detalle={str(meta.get('moreInfo', ''))[:800]}")
        data = result.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass
        return data

    async def async_update(self):
        async with self._lock:
            await self._refresh()

    async def _refresh(self):
        try:
            data = await self._request("SearchSubSystemArmStatus", {"operationMode": "all"})
            if not isinstance(data, list):
                raise HomeAssistantError("Formato de estado inesperado.")
            area = next((a for a in data if isinstance(a, dict) and str(a.get("subSystemID")) == str(self.area)), None)
            if area is None:
                raise HomeAssistantError("El área configurada no aparece en el panel.")
            raw = area.get("armStatus")
            state = {"disarmed": AlarmControlPanelState.DISARMED,
                     "arming": AlarmControlPanelState.ARMING}.get(raw)
            attrs = {"area_id": self.area, "device_serial": self.serial, "arm_status": raw, "axhome_cloud": True, "last_command_error": self._last_error}
            for key in ("systemFaults", "exitDelayTime"):
                if key in area:
                    attrs[key] = area[key]
            if raw == "armed":
                # The basic endpoint does not distinguish home from away.
                # Query the device, never infer the mode from a previous command.
                details = await self._request("SearchSubSystemStatus", {
                    "searchID": "ha_status", "searchResultPosition": 1,
                    "maxResults": 8, "searchBySubSystemIDs": [self.area],
                })
                rows = details.get("matchResults", []) if isinstance(details, dict) else []
                row = next((r for r in rows if isinstance(r, dict) and str(r.get("subSystemID")) == str(self.area)), {})
                mode = row.get("subSystemArmStatus")
                attrs["reported_mode"] = mode
                state = {"away": AlarmControlPanelState.ARMED_AWAY,
                         "stay": AlarmControlPanelState.ARMED_HOME,
                         "arming": AlarmControlPanelState.ARMING,
                         "disarm": AlarmControlPanelState.DISARMED}.get(mode)
                if row.get("alarmStatus") == "alarm":
                    state = AlarmControlPanelState.TRIGGERED
            self._attr_alarm_state = state
            self._attr_extra_state_attributes = attrs
            self._attr_available = True
        except HomeAssistantError as exc:
            self._attr_available = False
            self._attr_alarm_state = None
            _LOGGER.warning("AX HOME: %s", exc)

    async def _control(self, mode, clear=False, code=None):
        security = self.hass.data.get("axhome_cloud", {}).get("security")
        if security is None:
            raise HomeAssistantError("La protección por PIN no está disponible; orden bloqueada.")
        await security.verify(code)
        async with self._lock:
            payload = {"operationMode": "assigned", "subSystemIDList": [self.area]}
            action = "CtrlSubSystemDisarmList"
            if clear:
                action = "CtrlSubSystemClearAlarmList"
            elif mode is not None:
                action = "CtrlSubSystemArmList"
                payload["armType"] = mode
            try:
                result = await self._request(action, payload)
                self._last_error = None
            except HomeAssistantError as exc:
                self._last_error = str(exc)
                await self._refresh()
                self.async_write_ha_state()
                raise
            else:
                # Hik-Connect acknowledges the command before its status query
                # necessarily reflects the new state. Poll briefly here so HA
                # publishes the panel-confirmed state as soon as it is ready.
                expected = (None if clear else AlarmControlPanelState.DISARMED if mode is None
                            else AlarmControlPanelState.ARMED_HOME if mode == "stay"
                            else AlarmControlPanelState.ARMED_AWAY)
                for delay in ((0,) if expected is None else (0, 0.35, 0.65, 1.0)):
                    if delay:
                        await asyncio.sleep(delay)
                    await self._refresh()
                    self.async_write_ha_state()
                    if expected is not None and self._attr_alarm_state == expected:
                        break

    async def async_alarm_arm_away(self, code=None):
        await self._control("away", code=code)

    async def async_alarm_arm_home(self, code=None):
        await self._control("stay", code=code)

    async def async_alarm_disarm(self, code=None):
        await self._control(None, code=code)

    async def async_clear_alarm(self, code=None):
        await self._control(None, clear=True, code=code)
