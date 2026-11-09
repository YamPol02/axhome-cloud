"""Persistent PIN gate. Secrets are never exposed as entity attributes."""
import asyncio
import hashlib
import hmac
import re
import secrets
import time
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

class PinSecurity:
    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, "axhome_cloud.pin_security")
        self.data = {}
        self.lock = asyncio.Lock()

    async def load(self):
        self.data = await self.store.async_load() or {}

    @property
    def configured(self):
        return bool(self.data.get("salt") and self.data.get("digest"))

    def status(self):
        return {"configured": self.configured,
                "retry_after": max(0, int(self.data.get("locked_until", 0) - time.time()) + 1) if self.data.get("locked_until", 0) > time.time() else 0}

    @staticmethod
    def derive(code, salt):
        return hashlib.scrypt(code.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=67108864).hex()

    async def _check(self, code):
        if not self.configured:
            raise HomeAssistantError("Un administrador debe configurar el PIN de AX HOME antes de operar.")
        retry = self.status()["retry_after"]
        if retry:
            raise HomeAssistantError(f"Demasiados intentos. Espera {retry} segundos.")
        valid_format = isinstance(code, str) and re.fullmatch(r"[0-9]{6,12}", code)
        candidate = await self.hass.async_add_executor_job(self.derive, code if valid_format else "invalid", self.data["salt"])
        if not valid_format or not hmac.compare_digest(candidate, self.data["digest"]):
            self.data["failures"] = self.data.get("failures", 0) + 1
            if self.data["failures"] >= 5:
                self.data["locked_until"] = time.time() + 300
                self.data["failures"] = 0
            await self.store.async_save(self.data)
            raise HomeAssistantError("PIN incorrecto." if not self.status()["retry_after"] else "PIN incorrecto. Bloqueo de 5 minutos.")
        self.data["failures"] = 0
        self.data["locked_until"] = 0
        await self.store.async_save(self.data)

    async def verify(self, code):
        async with self.lock:
            await self._check(code)

    async def change(self, new_code, current_code=None):
        async with self.lock:
            if not isinstance(new_code, str) or re.fullmatch(r"[0-9]{6,12}", new_code) is None:
                raise HomeAssistantError("Usa entre 6 y 12 dígitos.")
            if self.configured:
                await self._check(current_code)
            salt = secrets.token_hex(32)
            digest = await self.hass.async_add_executor_job(self.derive, new_code, salt)
            updated = {"salt": salt, "digest": digest, "failures": 0, "locked_until": 0}
            await self.store.async_save(updated)
            self.data = updated
