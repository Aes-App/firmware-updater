"""Offline self-tests: everything that does NOT need a radio.

Covers RCSP framing round-trips (incl. a known vector), the block transfer
payloads, and a full mutual-auth handshake simulated between our host session
and a modelled JieLi device (both using the real emulated crypto).

    python -m bt_ota.selftest [path/to/ET25_QXDZ_Vxxxx.ufw]
"""
from __future__ import annotations

import sys

from . import rcsp
from .jl_auth import AuthEmulator, RcspAuthSession, AUTH_OK


def test_framing() -> None:
    # Known vector: RcspAuth.getResetAuthFlagCmdData() = "FEDCBAC00600020001EF"
    known = bytes.fromhex("FEDCBAC00600020001EF")
    built = rcsp.build_command(0x06, sn=0, param=b"\x01", has_response=True)
    assert built == known, f"framing vector mismatch: {built.hex()} != {known.hex()}"

    pkts = rcsp.PacketAssembler.parse_all(known)
    assert len(pkts) == 1
    p = pkts[0]
    assert p.type == 1 and p.opcode == 0x06 and p.sn == 0 and p.param == b"\x01"

    # command round-trip
    frame = rcsp.build_command(rcsp.CMD_GET_TARGET_INFO, sn=7, param=rcsp.get_target_info_param())
    (p,) = rcsp.PacketAssembler.parse_all(frame)
    assert p.opcode == rcsp.CMD_GET_TARGET_INFO and p.sn == 7
    assert p.param == bytes.fromhex("ffffffff00")

    # response round-trip (device->host response shape: status + sn + data)
    resp = rcsp.build_response(rcsp.CMD_OTA_ENTER_UPDATE_MODE, sn=9, status=0, param=b"\x00")
    (p,) = rcsp.PacketAssembler.parse_all(resp)
    assert p.type == 0 and p.opcode == rcsp.CMD_OTA_ENTER_UPDATE_MODE and p.sn == 9 and p.status == 0

    # fragmentation: split a frame across two feeds
    asm = rcsp.PacketAssembler()
    assert asm.feed(frame[:4]) == []
    got = asm.feed(frame[4:])
    assert len(got) == 1 and got[0].opcode == rcsp.CMD_GET_TARGET_INFO

    # two frames + leading garbage in one buffer
    got = rcsp.PacketAssembler.parse_all(b"\x11\x22" + known + resp)
    assert len(got) == 2
    print("  framing............... OK")


def test_block_payloads() -> None:
    # device pull request [offset:4][len:2] big-endian
    req = bytes.fromhex("0001000000f0")  # offset=0x00010000, len=0xF0
    off, length = rcsp.parse_block_request(req)
    assert off == 0x10000 and length == 0xF0, (off, length)
    # completion sentinel
    assert rcsp.parse_block_request(bytes(6)) == (0, 0)
    # content-size notify [size:4][progress:4]
    size, prog = rcsp.parse_content_size(bytes.fromhex("000b1a80" + "00000000"))
    assert size == 0x000B1A80 and prog == 0
    print("  block payloads........ OK")


class _SimDevice:
    """Minimal model of the radio side of the JieLi mutual auth (same key)."""

    def __init__(self, emu: AuthEmulator):
        self.emu = emu
        self.rand = bytes([0x00]) + bytes(range(0x20, 0x30))  # device nonce 00+Rd
        self.authenticated_host = False
        self._sent_challenge = False

    def on(self, data: bytes):
        if len(data) == 17 and data[0] == 0:            # host nonce -> prove ourselves
            return self.emu.get_encrypted_auth_data(data)
        if data == AUTH_OK and not self._sent_challenge:  # host confirmed us -> challenge host
            self._sent_challenge = True
            return self.rand
        if len(data) == 17 and data[0] == 1:            # host's proof for our challenge
            if data == self.emu.get_encrypted_auth_data(self.rand):
                self.authenticated_host = True
                return AUTH_OK
            raise AssertionError("host proof mismatch")
        return None


def test_auth_handshake(auth_lib: str | None = None) -> None:
    emu = AuthEmulator(auth_lib)
    host = RcspAuthSession(emu, nonce16=bytes(range(1, 17)))
    dev = _SimDevice(emu)

    # deterministic oracle
    v = emu.get_encrypted_auth_data(bytes([0] + list(range(1, 17))))
    assert v[0] == 1 and len(v) == 17
    assert emu.get_encrypted_auth_data(bytes([0] + list(range(1, 17)))) == v

    # run the exchange to a fixed point
    msg = host.initial_message()
    to_dev = True
    for _ in range(12):
        if to_dev:
            reply = dev.on(msg)
        else:
            reply = host.handle(msg)
        if reply is None:
            break
        msg = reply
        to_dev = not to_dev
    assert host.authenticated, "host did not reach authenticated state"
    assert dev.authenticated_host, "device did not authenticate the host"
    print("  auth handshake........ OK (mutual)")


def test_ufw(ufw_path: str | None, auth_lib: str | None = None) -> None:
    if not ufw_path:
        print("  ufw validation........ SKIP (no .ufw path given)")
        return
    ufw = open(ufw_path, "rb").read()
    emu = AuthEmulator(auth_lib)
    code = emu.validate_ufw(ufw)
    # -1 = bad CRC, -2 = truncated; anything else = header CRC + size OK
    assert code not in (-1, -2), f"native validator rejected file (code {code})"
    print(f"  ufw validation........ OK (native parse_fw_info code={code}, size={len(ufw)})")


def _wiced_crc32_reference(data: bytes) -> int:
    """The exact CRC32 from the decompiled WICED OtaUpgrader (reflected, poly 0x04C11DB7)."""
    def reflect(v, n):
        r = 0
        for i in range(n):
            if v & 1:
                r |= 1 << (n - 1 - i)
            v >>= 1
        return r
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= (reflect(b, 8) << 24) & 0xFFFFFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return (reflect(crc, 32) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def test_wiced() -> None:
    import zlib
    from . import wiced
    # command payloads match OtaUpgrader.sendCommand encoding (little-endian)
    size = 0x00012345
    assert wiced.WicedOta._u32le(size) == bytes.fromhex("45230100")
    prepare = bytes([wiced.CMD_PREPARE_DOWNLOAD]) + wiced.WicedOta._u32le(size)
    assert prepare == bytes.fromhex("01" + "45230100")
    assert bytes([wiced.CMD_DOWNLOAD]) == b"\x01\x02"[1:]  # [0x02]
    crc = 0xDEADBEEF
    assert bytes([wiced.CMD_VERIFY]) + wiced.WicedOta._u32le(crc) == bytes.fromhex("03efbeadde")
    # our zlib.crc32 matches the device's custom CRC32, on random-ish data
    sample = bytes((i * 37 + 11) & 0xFF for i in range(1000))
    assert (zlib.crc32(sample) & 0xFFFFFFFF) == _wiced_crc32_reference(sample)
    # 20-byte chunking covers the whole image
    n = 96362
    assert (n + wiced.CHUNK - 1) // wiced.CHUNK == 4819
    print("  wiced protocol........ OK (payloads + crc32 + chunking)")


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    ufw_path = argv[0] if argv else None
    print("bt_ota self-test")
    test_framing()
    test_block_payloads()
    test_auth_handshake()
    test_wiced()
    test_ufw(ufw_path)
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
