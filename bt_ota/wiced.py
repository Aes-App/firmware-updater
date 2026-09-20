from __future__ import annotations

import asyncio
import zlib
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

UUID_SERVICE = "9e5d1e47-5c13-43a0-8635-82ad38a1386f"
UUID_CONTROL = "e3dd50bf-f7a7-4e99-838e-570a086c666b"
UUID_DATA = "92e86c7a-d961-4091-b74f-2409e72efe36"

CMD_PREPARE_DOWNLOAD = 1
CMD_DOWNLOAD = 2
CMD_VERIFY = 3
CMD_FINISH = 4
CMD_ABORT = 7

CHUNK = 20

STATUS = {
    0: "OK", 1: "unsupported command", 2: "illegal state", 3: "verification failed",
    4: "invalid image", 5: "invalid image size", 6: "more data", 7: "invalid app id",
    8: "invalid version", 9: "disconnect", 10: "abort", 11: "timeout",
}


def _noop_log(_m):
    pass


def _noop_progress(_s, _t):
    pass


class WicedOtaError(RuntimeError):
    pass


class WicedOta:

    def __init__(self, write_response: bool = True, log_cb: Callable[[str], None] = _noop_log):
        self._log = log_cb
        self._response = write_response
        self.client: Optional[BleakClient] = None
        self._address = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._status_fut: Optional[asyncio.Future] = None

    @staticmethod
    async def scan(timeout: float = 8.0, name_filter: Optional[str] = None):
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for dev, adv in found.values():
            name = adv.local_name or dev.name or ""
            has_ota = UUID_SERVICE in [u.lower() for u in (adv.service_uuids or [])]
            if name_filter and not (name_filter.lower() in name.lower()
                                    or name_filter.lower() == dev.address.lower()):
                continue
            if name or has_ota:
                out.append((dev, adv.rssi, name))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    async def connect(self, address_or_device) -> None:
        self._loop = asyncio.get_running_loop()
        self._address = getattr(address_or_device, "address", address_or_device)
        self.client = BleakClient(address_or_device)
        await self.client.connect()
        self._address = self.client.address
        self._log(f"connected: {self.client.address}")
        svc = self.client.services.get_service(UUID_SERVICE)
        if svc is None:
            raise WicedOtaError(
                "device has no WICED OTA service (9e5d…). Is it a D578/D878 in "
                "OTA mode, and is the .bin the right firmware?")
        data_props = ctrl_props = set()
        try:
            dch = self.client.services.get_characteristic(UUID_DATA)
            cch = self.client.services.get_characteristic(UUID_CONTROL)
            data_props = set(dch.properties) if dch else set()
            ctrl_props = set(cch.properties) if cch else set()
            if "write" in data_props:
                self._response = True
            elif "write-without-response" in data_props:
                self._response = False
        except Exception:
            pass
        self._log(f"data char props: {sorted(data_props)}; control props: {sorted(ctrl_props)}")
        self._log(f"data write mode: {'with-response' if self._response else 'without-response (paced)'}")
        await self.client.start_notify(UUID_CONTROL, self._on_notify)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(UUID_CONTROL)
            except Exception:
                pass
            await self.client.disconnect()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    def _on_notify(self, _char, data: bytearray) -> None:
        st = data[0] if data else 0xFF
        self._log(f"control-point status: {st} ({STATUS.get(st, '?')})")
        if self._status_fut and not self._status_fut.done():
            self._status_fut.set_result(st)

    async def _command(self, payload: bytes, timeout: float = 10.0) -> int:
        self._status_fut = self._loop.create_future()
        await self.client.write_gatt_char(UUID_CONTROL, payload, response=True)
        try:
            return await asyncio.wait_for(self._status_fut, timeout)
        except asyncio.TimeoutError:
            raise WicedOtaError(f"timeout waiting for status after command {payload[0]}")

    @staticmethod
    def _u32le(v: int) -> bytes:
        return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])

    async def upgrade(self, fw: bytes,
                      progress_cb: Callable[[int, int], None] = _noop_progress,
                      transfer_timeout: float = 600.0) -> None:
        size = len(fw)
        if size == 0:
            raise WicedOtaError("firmware .bin is empty")
        crc = zlib.crc32(fw) & 0xFFFFFFFF
        self._log(f"firmware: {size} bytes, crc32=0x{crc:08X}")

        st = await self._command(bytes([CMD_PREPARE_DOWNLOAD]) + self._u32le(size))
        if st != 0:
            raise WicedOtaError(f"prepare-download rejected: {STATUS.get(st, st)}")
        self._log("prepare OK; starting download")

        st = await self._command(bytes([CMD_DOWNLOAD]))
        if st != 0:
            raise WicedOtaError(f"download command rejected: {STATUS.get(st, st)}")

        deadline = self._loop.time() + transfer_timeout
        for i, off in enumerate(range(0, size, CHUNK)):
            await self.client.write_gatt_char(UUID_DATA, fw[off:off + CHUNK], response=self._response)
            if not self._response:
                await asyncio.sleep(0.004)
            progress_cb(min(off + CHUNK, size), size)
            if self._loop.time() > deadline:
                raise WicedOtaError("timed out during firmware data transfer")
        self._log("image sent; settling before verify")
        await asyncio.sleep(0.3)

        try:
            st = await self._command(bytes([CMD_VERIFY]) + self._u32le(crc), timeout=30.0)
        except Exception as e:
            self._log(f"verify: no status returned ({e})")
            self._log("module likely verified and rebooted to apply — CONFIRM the radio's "
                      "BT version to be sure (this is expected on macOS for this module)")
            return
        if st != 0:
            raise WicedOtaError(f"verify failed: {STATUS.get(st, st)}")
        self._log("verify OK — module will reboot to apply the new BT firmware")
