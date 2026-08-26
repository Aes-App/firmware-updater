"""AnyTone / JieLi Bluetooth-LE OTA client.

Drives the same RCSP OTA flow the AnyTone phone app performs, over the host's
own Bluetooth adapter via `bleak`, so no Android phone is needed.

Flow (ported from BluetoothOTAManager):
  connect -> subscribe AE02 -> [mutual auth on AE01] -> GetTargetInfo(0x03)
  -> GetUpdateFileOffset(0xE1) -> read ufw[offset:offset+len]
  -> InquireUpdate(0xE2, flag) -> EnterUpdateMode(0xE3)
  -> device drives: NotifyContentSize(0xE8) + FirmwareBlock pulls(0xE5, we answer
     with raw ufw bytes) until it requests offset=0,len=0 (complete)
  -> QueryUpdateResult(0xE6) -> (device reboots and applies).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from bleak import BleakClient, BleakScanner

from . import rcsp
from .jl_auth import AuthEmulator, RcspAuthSession

UUID_SERVICE = "0000ae00-0000-1000-8000-00805f9b34fb"
UUID_WRITE = "0000ae01-0000-1000-8000-00805f9b34fb"
UUID_NOTIFY = "0000ae02-0000-1000-8000-00805f9b34fb"

DEFAULT_NAME_PREFIXES = ("ET25SE_BLE", "ET25", "D890", "D578", "D168", "AT-D")

# InquireUpdate (0xE2) result codes (BluetoothOTAManager.upgradeStep02)
INQUIRE_RESULT = {
    0: "OK - device will update",
    1: "device busy / not ready",
    2: "upgrade-file check failed",
    3: "firmware already up to date",
    4: "TWS peer not connected",
    5: "not in charging case",
}


def _noop_log(msg: str) -> None:
    pass


def _noop_progress(sent: int, total: int) -> None:
    pass


class OtaError(RuntimeError):
    pass


class AnytoneBtOta:
    def __init__(
        self,
        auth_lib: Optional[str] = None,
        use_auth: bool = True,
        write_without_response: bool = True,
        log_cb: Callable[[str], None] = _noop_log,
    ):
        self._use_auth = use_auth
        self._auth_lib = auth_lib
        self._wwr = write_without_response
        self._log = log_cb
        self.client: Optional[BleakClient] = None
        self._address = None
        self._assembler = rcsp.PacketAssembler()
        self._auth: Optional[RcspAuthSession] = None
        self._emu: Optional[AuthEmulator] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._sn = 0
        self._mtu = 20
        self._write_char = UUID_WRITE
        # OTA transfer state
        self._ufw = b""
        self._content_size = 0
        self._sent = 0
        self._progress: Callable[[int, int], None] = _noop_progress
        self._transfer_done: Optional[asyncio.Future] = None
        self._transfer_error: Optional[Exception] = None
        self._transfer_started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- discovery -----------------------------------------------------------
    @staticmethod
    async def scan(timeout: float = 8.0, name_filter: Optional[str] = None):
        """Return candidate radios as list of (device, rssi, name)."""
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for dev, adv in found.values():
            name = adv.local_name or dev.name or ""
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            is_candidate = UUID_SERVICE in uuids or any(
                name.upper().startswith(p.upper()) for p in DEFAULT_NAME_PREFIXES
            )
            if name_filter:
                is_candidate = name_filter.lower() in name.lower() or name_filter.lower() == dev.address.lower()
            if is_candidate:
                out.append((dev, adv.rssi, name))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    # -- connection ----------------------------------------------------------
    async def connect(self, address_or_device) -> None:
        self._loop = asyncio.get_running_loop()
        self._address = getattr(address_or_device, "address", address_or_device)
        self.client = BleakClient(address_or_device)
        await self.client.connect()
        self._address = self.client.address
        self._log(f"connected: {self.client.address}")
        try:
            self._mtu = max(self.client.mtu_size, 23)
        except Exception:
            self._mtu = 23
        self._log(f"ATT MTU: {self._mtu}")
        # pick a write mode the AE01 characteristic actually supports
        try:
            ch = self.client.services.get_characteristic(UUID_WRITE)
            props = set(ch.properties) if ch else set()
            if self._wwr and "write-without-response" not in props:
                self._wwr = False
            elif not self._wwr and "write" not in props and "write-without-response" in props:
                self._wwr = True
        except Exception:
            pass
        # fresh per-link state (important on reconnect after a reboot)
        self._assembler = rcsp.PacketAssembler()
        self._pending.clear()
        self._auth = None
        self._auth_future = None
        await self.client.start_notify(UUID_NOTIFY, self._on_notify)
        if self._use_auth:
            await self._authenticate()

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(UUID_NOTIFY)
            except Exception:
                pass
            await self.client.disconnect()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    # -- low level I/O -------------------------------------------------------
    async def _write_raw(self, data: bytes) -> None:
        chunk = max(self._mtu - 3, 20)
        for i in range(0, len(data), chunk):
            await self.client.write_gatt_char(
                self._write_char, data[i:i + chunk], response=not self._wwr
            )

    def _next_sn(self) -> int:
        self._sn = (self._sn % 255) + 1  # 1..255
        return self._sn

    def _on_notify(self, _char, data: bytearray) -> None:
        data = bytes(data)
        # Auth messages are raw (not RCSP framed); route by shape while auth pending.
        if self._auth is not None and not self._auth.authenticated and RcspAuthSession.is_auth_data(data):
            try:
                reply = self._auth.handle(data)
            except Exception as e:  # auth mismatch
                self._fail_transfer(e)
                fut = self._auth_future
                if fut and not fut.done():
                    fut.set_exception(e)
                return
            if reply is not None:
                asyncio.ensure_future(self._write_raw(reply))
            if self._auth.authenticated:
                fut = self._auth_future
                if fut and not fut.done():
                    fut.set_result(True)
            return
        for pkt in self._assembler.feed(data):
            self._dispatch(pkt)

    def _dispatch(self, pkt: rcsp.Packet) -> None:
        if pkt.is_response:
            fut = self._pending.pop(pkt.sn, None)
            if fut and not fut.done():
                fut.set_result(pkt)
            return
        # device-initiated command -> we must answer
        if pkt.opcode == rcsp.CMD_OTA_SEND_FIRMWARE_UPDATE_BLOCK:
            self._handle_block_request(pkt)
        elif pkt.opcode == rcsp.CMD_OTA_NOTIFY_UPDATE_CONTENT_SIZE:
            self._handle_content_size(pkt)
        elif pkt.opcode == rcsp.CMD_SETTINGS_COMMUNICATION_MTU:
            asyncio.ensure_future(self._write_raw(
                rcsp.build_response(pkt.opcode, pkt.sn, status=0, param=pkt.param)))
        # else: ignore (e.g. 0xC2 adv notify)

    # -- device-driven OTA handlers -----------------------------------------
    def _handle_block_request(self, pkt: rcsp.Packet) -> None:
        self._transfer_started = True
        offset, length = rcsp.parse_block_request(pkt.param)
        if offset == 0 and length == 0:
            # transfer complete
            asyncio.ensure_future(self._write_raw(
                rcsp.build_response(pkt.opcode, pkt.sn, status=0)))
            if self._transfer_done and not self._transfer_done.done():
                self._transfer_done.set_result(True)
            return
        end = offset + length
        if end > len(self._ufw):
            self._fail_transfer(OtaError(
                f"device requested [{offset}:{end}] beyond ufw ({len(self._ufw)} bytes)"))
            asyncio.ensure_future(self._write_raw(
                rcsp.build_response(pkt.opcode, pkt.sn, status=1)))
            return
        block = self._ufw[offset:end]
        self._sent += length
        self._progress(min(self._sent, self._content_size or self._sent),
                       self._content_size or len(self._ufw))
        asyncio.ensure_future(self._write_raw(
            rcsp.build_response(pkt.opcode, pkt.sn, status=0, param=block)))

    def _handle_content_size(self, pkt: rcsp.Packet) -> None:
        self._transfer_started = True
        size, progress = rcsp.parse_content_size(pkt.param)
        self._content_size = size
        self._sent = progress
        self._log(f"content size = {size} bytes, resume at {progress}")
        asyncio.ensure_future(self._write_raw(
            rcsp.build_response(pkt.opcode, pkt.sn, status=0)))

    def _fail_transfer(self, exc: Exception) -> None:
        self._transfer_error = exc
        if self._transfer_done and not self._transfer_done.done():
            self._transfer_done.set_exception(exc)

    # -- request/response helper --------------------------------------------
    async def _command(self, opcode: int, param: bytes = b"", timeout: float = 8.0) -> rcsp.Packet:
        sn = self._next_sn()
        fut = self._loop.create_future()
        self._pending[sn] = fut
        await self._write_raw(rcsp.build_command(opcode, sn, param))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(sn, None)
            raise OtaError(f"timeout waiting for response to opcode 0x{opcode:02X}")

    # -- auth ----------------------------------------------------------------
    async def _authenticate(self, timeout: float = 8.0) -> None:
        if self._emu is None:
            self._emu = AuthEmulator(self._auth_lib)
        self._auth = RcspAuthSession(self._emu)
        self._auth_future = self._loop.create_future()
        self._log("authenticating (JieLi RCSP mutual challenge/response)...")
        await self._write_raw(self._auth.initial_message())
        try:
            await asyncio.wait_for(self._auth_future, timeout)
        except asyncio.TimeoutError:
            raise OtaError(
                "auth timed out. The device may not require auth (try --no-auth), "
                "or the link key differs from this firmware's default.")
        self._log("auth OK")

    _auth_future: Optional[asyncio.Future] = None

    # -- high level ----------------------------------------------------------
    async def get_target_info(self) -> rcsp.TargetInfo:
        resp = await self._command(rcsp.CMD_GET_TARGET_INFO, rcsp.get_target_info_param())
        self.last_target_info_raw = resp.param
        return rcsp.parse_target_info(resp.param)

    last_target_info_raw: bytes = b""

    async def upgrade(
        self,
        ufw: bytes,
        progress_cb: Callable[[int, int], None] = _noop_progress,
        transfer_timeout: float = 600.0,
        max_reconnects: int = 3,
    ) -> None:
        if len(ufw) < 64:
            raise OtaError("ufw file too small to be valid")
        self._ufw = ufw
        self._progress = progress_cb

        info = await self.get_target_info()
        single = not info.support_double_backup
        self._log(f"device: fw={info.firmware_version_name or 'n/a'} sdk={info.sdk_type} "
                  f"backup={'single' if single else 'double'} "
                  f"needBootLoader={info.need_boot_loader} rcsp_mtu={info.communication_mtu}")
        if single:
            self._log("single-backup: after the transfer the module reboots into its loader; "
                      "the tool will reconnect and resume automatically.")

        pass_no = 0
        reconnects = 0
        while True:
            pass_no += 1
            self._log(f"--- OTA pass {pass_no} ---")
            result = await self._run_ota_pass(transfer_timeout)

            if result == 0:
                self._log("update result: 0 (success)")
                try:
                    await self._command(rcsp.CMD_REBOOT_DEVICE, rcsp.reboot_param(0), timeout=5.0)
                except OtaError:
                    pass  # device reboots without replying - expected
                self._log("done - module rebooting to apply the new BT firmware")
                return

            need_reconnect = (result == 128) or (result is None and single)
            if need_reconnect:
                reconnects += 1
                if reconnects > max_reconnects:
                    raise OtaError(f"gave up after {max_reconnects} reconnect attempts")
                why = "status 128 (data staged; reboot to apply)" if result == 128 \
                    else "device rebooted without a status reply"
                self._log(f"{why}; rebooting module + reconnecting "
                          f"(attempt {reconnects}/{max_reconnects})...")
                await self._reboot_and_reconnect(way=0)
                continue

            _RESULT = {1: "received-data check failed", 2: "device reported update failed",
                       3: "upgrade key / firmware does not match this device"}
            raise OtaError(f"update failed (code {result}: {_RESULT.get(result, 'unknown')})")

    async def _run_ota_pass(self, transfer_timeout: float):
        """One connection's worth of OTA. Returns the 0xE6 status code, or None if
        the device rebooted without answering the status query."""
        ufw = self._ufw
        self._sent = 0
        self._content_size = 0
        self._transfer_error = None

        # where the device wants the 'update file flag' bytes from
        off_resp = await self._command(rcsp.CMD_OTA_GET_DEVICE_UPDATE_FILE_INFO_OFFSET)
        flag_offset = int.from_bytes(off_resp.param[0:4], "big") if len(off_resp.param) >= 4 else 0
        flag_len = int.from_bytes(off_resp.param[4:6], "big") if len(off_resp.param) >= 6 else 0
        if flag_offset == 0 and flag_len == 0:
            # No file-flag region: inquire carries a single priority byte (0 = BLE).
            # (BluetoothOTAManager.upgradeStep02: intToByte(getPriority()))
            flag = bytes([0])
            self._log("file-flag region: none (0,0) -> inquire with priority byte 0x00")
        else:
            flag = ufw[flag_offset:flag_offset + flag_len]
            self._log(f"file-flag region: offset={flag_offset} len={flag_len}")

        inq = await self._command(rcsp.CMD_OTA_INQUIRE_DEVICE_IF_CAN_UPDATE, flag)
        code = inq.param[0] if inq.param else 0xFF
        self._log(f"inquire: {code} ({INQUIRE_RESULT.get(code, 'unknown')})")
        if code == 3:
            raise OtaError("device firmware already up to date")
        if code != 0:
            raise OtaError(f"device refused update: {INQUIRE_RESULT.get(code, code)}")

        # arm completion waiter BEFORE entering update mode (device pulls immediately)
        self._transfer_done = self._loop.create_future()
        self._transfer_started = False

        # Enter update mode. Some modules reply with a can-update flag; this one just
        # starts pulling blocks with no discrete reply - tolerate both.
        enter_sn = self._next_sn()
        enter_fut = self._loop.create_future()
        self._pending[enter_sn] = enter_fut
        await self._write_raw(rcsp.build_command(rcsp.CMD_OTA_ENTER_UPDATE_MODE, enter_sn))
        try:
            enter = await asyncio.wait_for(enter_fut, timeout=6.0)
            eflag = enter.param[0] if enter.param else 0
            if eflag != 0:
                raise OtaError(f"device declined to enter update mode (flag={eflag})")
            self._log("update mode entered; streaming firmware (device-driven)...")
        except asyncio.TimeoutError:
            self._pending.pop(enter_sn, None)
            if not self._transfer_started:
                raise OtaError("no reply to enter-update-mode and no transfer started")
            self._log("update mode entered (no explicit reply; transfer already underway)...")

        try:
            await asyncio.wait_for(self._transfer_done, transfer_timeout)
        except asyncio.TimeoutError:
            raise OtaError("timed out during firmware block transfer")
        if self._transfer_error:
            raise self._transfer_error
        self._progress(self._content_size or len(ufw), self._content_size or len(ufw))
        self._log("block transfer complete; querying status...")

        try:
            status = await self._command(rcsp.CMD_OTA_GET_DEVICE_REFRESH_FIRMWARE_STATUS, timeout=15.0)
            return status.param[0] if status.param else 0
        except OtaError:
            return None  # device rebooted without answering

    async def _reboot_and_reconnect(self, way: int = 0, settle: float = 3.0,
                                    attempts: int = 8) -> None:
        """Single-backup finish: tell the module to reboot for reconnect (0x0B),
        drop the link, then reconnect (same address, else rescan by name) + re-auth."""
        address = self._address
        # 0x0B NotifyCommunicationWay [way, reconnect=1] - triggers the reboot
        try:
            await self._command(rcsp.CMD_SWITCH_DEVICE_REQUEST, bytes([way & 0xFF, 1]), timeout=5.0)
        except Exception:  # noqa: BLE001 - device may drop the link as it reboots
            pass
        try:
            await self.disconnect()
        except Exception:
            pass
        await asyncio.sleep(settle)
        last_err: Optional[Exception] = None
        for i in range(attempts):
            # 1) try the same address (loader keeps the BLE identity in the BLE case)
            try:
                self._log(f"reconnecting to {address} ({i + 1}/{attempts})...")
                await self.connect(address)
                self._log("reconnected + re-authenticated")
                return
            except Exception as e:  # noqa: BLE001 - retry any BLE failure
                last_err = e
            # 2) fall back to a rescan by name (address may have changed)
            try:
                cands = await self.scan(timeout=5.0)
                if cands:
                    self._log(f"address failed; found {cands[0][2]} by scan, connecting...")
                    await self.connect(cands[0][0])
                    self._log("reconnected + re-authenticated")
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
            await asyncio.sleep(settle)
        raise OtaError(f"failed to reconnect after reboot: {last_err}")
