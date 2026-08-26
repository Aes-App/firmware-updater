"""JieLi RCSP wire protocol (framing + OTA opcodes).

Frame layout (verified against ParseHelper.packSendBasePacket / findPacketData):

    FE DC BA | flags | opcode | paramLen(2, big-endian) | payload | EF

    flags: bit7 = type (1 = command from host, 0 = response), bit6 = need-response
    payload (command, type=1):  sn [+ xmOpcode if opcode==1] + paramData
    payload (response, type=0):  status + sn [+ xmOpcode if opcode==1] + paramData
    paramLen counts the payload only (not prefix/header/terminator)

All multi-byte integers in RCSP are big-endian (CHexConver.bytesToInt).
"""
from __future__ import annotations

from dataclasses import dataclass, field

PREFIX = bytes([0xFE, 0xDC, 0xBA])
TERMINATOR = 0xEF

# --- opcodes (com.jieli.jl_bt_ota.constant.Command) --------------------------
CMD_DATA = 0x01
CMD_GET_TARGET_FEATURE_MAP = 0x02
CMD_GET_TARGET_INFO = 0x03
CMD_SWITCH_DEVICE_REQUEST = 0x0B                # 11  NotifyCommunicationWay (reconnect)
CMD_SETTINGS_COMMUNICATION_MTU = 0xD1          # 209  (device->host during OTA)
CMD_GET_DEV_MD5 = 0xD4                          # 212
CMD_OTA_GET_DEVICE_UPDATE_FILE_INFO_OFFSET = 0xE1  # 225
CMD_OTA_INQUIRE_DEVICE_IF_CAN_UPDATE = 0xE2    # 226
CMD_OTA_ENTER_UPDATE_MODE = 0xE3               # 227
CMD_OTA_EXIT_UPDATE_MODE = 0xE4                # 228
CMD_OTA_SEND_FIRMWARE_UPDATE_BLOCK = 0xE5      # 229  (device->host pull)
CMD_OTA_GET_DEVICE_REFRESH_FIRMWARE_STATUS = 0xE6  # 230
CMD_REBOOT_DEVICE = 0xE7                        # 231
CMD_OTA_NOTIFY_UPDATE_CONTENT_SIZE = 0xE8      # 232  (device->host notify)

# GetTargetInfo attribute types (AttrAndFunCode) we decode
_ATTR = {
    0: "protocol_version", 4: "function_info", 5: "firmware", 6: "sdk_type",
    7: "uboot", 8: "double_backup", 9: "mandatory", 10: "vidpid",
    11: "auth_key", 12: "project_code", 13: "mtu", 16: "name", 17: "ble",
    19: "dev_support",
}


def _be16(v: int) -> bytes:
    return bytes([(v >> 8) & 0xFF, v & 0xFF])


def _be32(v: int) -> bytes:
    return bytes([(v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])


@dataclass
class Packet:
    type: int                # 1 = command (from device), 0 = response (from device)
    has_response: int
    opcode: int
    sn: int
    status: int = 0
    xm_opcode: int | None = None
    param: bytes = b""

    @property
    def is_response(self) -> bool:
        return self.type == 0


def build_command(opcode: int, sn: int, param: bytes = b"",
                  has_response: bool = True, xm_opcode: int | None = None) -> bytes:
    """Host -> device command frame (type=1)."""
    payload = bytearray([sn & 0xFF])
    if opcode == CMD_DATA and xm_opcode is not None:
        payload.append(xm_opcode & 0xFF)
    payload += param
    flags = 0x80 | (0x40 if has_response else 0x00)
    return _frame(flags, opcode, payload)


def build_response(opcode: int, sn: int, status: int = 0, param: bytes = b"",
                   xm_opcode: int | None = None) -> bytes:
    """Host -> device response frame (type=0), e.g. answering a block pull."""
    payload = bytearray([status & 0xFF, sn & 0xFF])
    if opcode == CMD_DATA and xm_opcode is not None:
        payload.append(xm_opcode & 0xFF)
    payload += param
    return _frame(0x00, opcode, payload)


def _frame(flags: int, opcode: int, payload: bytes) -> bytes:
    return bytes(PREFIX) + bytes([flags, opcode]) + _be16(len(payload)) + bytes(payload) + bytes([TERMINATOR])


class PacketAssembler:
    """Streaming reassembler for RCSP frames (BLE notifications arrive chunked)."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self._buf += data
        out: list[Packet] = []
        while True:
            pkt, consumed = self._try_one()
            if consumed == 0:
                break
            del self._buf[:consumed]
            if pkt is not None:
                out.append(pkt)
        return out

    def _try_one(self):
        buf = self._buf
        start = buf.find(PREFIX)
        if start < 0:
            # keep only a possible partial prefix at the tail
            keep = 0
            for k in (2, 1):
                if buf[-k:] == PREFIX[:k]:
                    keep = k
                    break
            if len(buf) > keep:
                del buf[:len(buf) - keep]
            return None, 0
        if len(buf) < start + 7:               # need prefix+flags+opcode+len(2)
            return None, start if start else 0
        param_len = (buf[start + 5] << 8) | buf[start + 6]
        total = start + 3 + 4 + param_len + 1   # +terminator
        if len(buf) < total:
            return None, start if start else 0
        if buf[total - 1] != TERMINATOR:
            return None, start + 1              # false prefix, resync past it
        pkt = _parse(bytes(buf[start + 3: total - 1]))
        return pkt, total

    # convenience for tests / one-shot parsing
    @staticmethod
    def parse_all(data: bytes) -> list[Packet]:
        return PacketAssembler().feed(data)


def _parse(body: bytes) -> Packet | None:
    """body = flags..paramData (prefix and terminator stripped)."""
    if len(body) < 4:
        return None
    flags = body[0]
    opcode = body[1]
    param_len = (body[2] << 8) | body[3]
    typ = (flags >> 7) & 1
    has_resp = (flags >> 6) & 1
    pkt = Packet(type=typ, has_response=has_resp, opcode=opcode, sn=0)
    if param_len <= 0:
        return pkt
    i = 4
    if typ == 0:
        pkt.status = body[i]
        i += 1
    pkt.sn = body[i]
    i += 1
    if opcode == CMD_DATA:
        pkt.xm_opcode = body[i]
        i += 1
    pkt.param = body[i: 4 + param_len]
    return pkt


# --- specific command payload builders --------------------------------------
def get_target_info_param(mask: int = 0xFFFFFFFF, platform: int = 0) -> bytes:
    return _be32(mask) + bytes([platform & 0xFF])


def reboot_param(flag: int = 0) -> bytes:
    return bytes([flag & 0xFF])


# --- response parsers --------------------------------------------------------
def parse_block_request(param: bytes) -> tuple[int, int]:
    """0xE5 device->host: [offset:4 BE][len:2 BE]. (0,0) means transfer complete."""
    if len(param) < 6:
        return 0, 0
    offset = int.from_bytes(param[0:4], "big")
    length = int.from_bytes(param[4:6], "big")
    return offset, length


def parse_content_size(param: bytes) -> tuple[int, int]:
    """0xE8 device->host: [contentSize:4 BE][currentProgress:4 BE]."""
    if len(param) < 4:
        return 0, 0
    size = int.from_bytes(param[0:4], "big")
    progress = int.from_bytes(param[4:8], "big") if len(param) >= 8 else 0
    return size, progress


def parse_mtu(param: bytes) -> int:
    return int.from_bytes(param[0:2], "big") if len(param) >= 2 else 0


@dataclass
class TargetInfo:
    protocol_version: str = ""
    firmware_version_code: int = 0
    firmware_version_name: str = ""
    uboot_version_name: str = ""
    sdk_type: int = 0
    support_double_backup: bool = False
    need_boot_loader: bool = False
    single_backup_ota_way: int = 0
    mandatory_upgrade: int = 0
    request_ota: int = 0
    vid: int = 0
    pid: int = 0
    uid: int = 0
    name: str = ""
    ble_addr: str = ""
    edr_addr: str = ""
    communication_mtu: int = 0
    auth_key: str = ""
    project_code: str = ""
    raw: dict = field(default_factory=dict)


def parse_target_info(param: bytes) -> TargetInfo:
    """Port of ParseHelper.parseTargetInfo TLV walk (records: [len][type][data])."""
    info = TargetInfo()
    i, n = 0, len(param)
    while i + 2 <= n:
        rec_len = param[i]
        if rec_len <= 0 or rec_len >= n:
            break
        typ = param[i + 1]
        dlen = rec_len - 1
        if dlen < 0 or i + 2 + dlen > n:
            break
        data = param[i + 2: i + 2 + dlen]
        info.raw[_ATTR.get(typ, f"type_{typ}")] = data.hex()
        try:
            _fill_target_field(info, typ, data)
        except (IndexError, UnicodeDecodeError):
            pass
        i += rec_len + 1
    return info


def _fill_target_field(info: TargetInfo, typ: int, d: bytes) -> None:
    if typ == 0 and d:
        info.protocol_version = f"V_{(d[0] >> 4) & 0xF}.{d[0] & 0xF}"
    elif typ == 5 and len(d) >= 2:
        code = (d[0] << 8) | d[1]
        info.firmware_version_code = code
        info.firmware_version_name = (
            f"V_{(code >> 12) & 0xF}.{(code >> 8) & 0xF}.{(code >> 4) & 0xF}.{code & 0xF}"
        )
    elif typ == 2 and len(d) >= 6:
        info.edr_addr = ":".join(f"{b:02X}" for b in d[0:6])
    elif typ == 6 and d:
        info.sdk_type = d[0]
    elif typ == 7:
        if len(d) == 2:  # packed nibble version (ParseHelper case 7)
            info.uboot_version_name = f"{d[0] >> 4}.{d[0] & 0xF}.{d[1] >> 4}.{d[1] & 0xF}"
        else:
            info.uboot_version_name = _safe_str(d)
    elif typ == 8 and d:
        info.support_double_backup = d[0] == 1
        if len(d) >= 2:
            info.need_boot_loader = d[1] == 1
        if len(d) >= 3:
            info.single_backup_ota_way = d[2]
    elif typ == 9 and d:
        info.mandatory_upgrade = d[0]
        if len(d) >= 2:
            info.request_ota = d[1]
    elif typ == 10 and len(d) >= 4:
        info.vid = (d[0] << 8) | d[1]
        info.pid = (d[2] << 8) | d[3]
        if len(d) >= 6:
            info.uid = (d[4] << 8) | d[5]
    elif typ == 11:
        info.auth_key = _safe_str(d)
    elif typ == 12:
        info.project_code = _safe_str(d)
    elif typ == 13 and len(d) >= 2:
        info.communication_mtu = (d[0] << 8) | d[1]
    elif typ == 16:
        info.name = _safe_str(d)
    elif typ == 17 and len(d) >= 7:
        info.ble_addr = ":".join(f"{b:02X}" for b in d[1:7])


def _safe_str(d: bytes) -> str:
    return d.split(b"\x00", 1)[0].decode("latin-1", "replace")
