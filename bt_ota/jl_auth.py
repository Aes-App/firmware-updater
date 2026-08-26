"""JieLi (JL) RCSP OTA authentication.

The AnyTone "OTA" phone app authenticates to the radio's JieLi Bluetooth module
with a mutual challenge/response before it will accept any OTA command. The
crypto lives in a tiny native library shipped inside the app,
``libjl_ota_auth.so`` (``com.jieli.jl_bt_ota.impl.RcspAuth``). The app never
calls ``setLinkKey``, so the key is a fixed constant baked into that ``.so``.

Rather than re-implement the (SAFER+/Bluetooth-E1 derived) cipher by hand, we
run the *real* ``function_E1test`` from the extracted ``.so`` under the Unicorn
CPU emulator. It only touches its arguments, a handful of libc calls
(malloc/free/memcpy/…) and its own static ``.data`` constants, so emulation is
byte-exact by construction and fully host-portable (no ARM hardware needed).

Provenance: extract ``lib/arm64-v8a/libjl_ota_auth.so`` from the AnyTone OTA
APK you already have (``python -m bt_ota.extract_auth_lib OTA.apk``). It is used
purely for interoperability with a radio you own.
"""
from __future__ import annotations

import os
import struct
import sys


def _prefer_bundled_unicorn() -> None:
    """PyInstaller-frozen Windows builds: force unicorn to load ITS OWN bundled
    native DLL via LIBUNICORN_PATH (unicorn's first-choice loader hook). Without
    this, unicorn's loader can fall through to a mismatched *system* libunicorn —
    e.g. an old 1.0.2 shipped by another tool (speakeasy) — and then a 2.1.x
    Python layer calling 1.0.2's uc_mem_map access-violates. No-op unless frozen
    on Windows and not already overridden."""
    mei = getattr(sys, "_MEIPASS", None)
    if not mei or sys.platform not in ("win32", "cygwin") or os.environ.get("LIBUNICORN_PATH"):
        return
    import glob
    hits = glob.glob(os.path.join(mei, "**", "unicorn.dll"), recursive=True)
    if hits:
        os.environ["LIBUNICORN_PATH"] = os.path.dirname(hits[0])


_prefer_bundled_unicorn()

try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UcError
    from unicorn.arm64_const import (
        UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3,
        UC_ARM64_REG_SP, UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_TPIDR_EL0,
    )
    _HAVE_UNICORN = True
except ImportError:  # pragma: no cover - surfaced with a friendly message in CLI
    _HAVE_UNICORN = False


# ---- addresses inside libjl_ota_auth.so (arm64 build, verified via objdump) --
FUNCTION_E1TEST = 0x1364      # core keyed transform
REAL_DECRYPT = 0x2250        # internal, reached via PLT from parse_fw_info
PARSE_FW_INFO = 0x2384       # optional .ufw validator
# PLT stubs we intercept (their GOT entries are unresolved under emulation)
_PLT = {
    0x3220: "malloc", 0x3230: "free", 0x3240: "__stack_chk_fail",
    0x3260: "decrypt", 0x3280: "__strlen_chk", 0x3290: "memcmp", 0x32a0: "memcpy",
}

DEFAULT_SO_NAME = "libjl_ota_auth.so"

# Auth wire constants (from RcspAuth.java)
AUTH_OK = bytes([2, 0x70, 0x61, 0x73, 0x73])   # 02 'p' 'a' 's' 's'


def _default_so_path() -> str:
    env = os.environ.get("JL_OTA_AUTH_SO")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, DEFAULT_SO_NAME)]
    mei = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle root
    if mei:
        candidates += [os.path.join(mei, "bt_ota", DEFAULT_SO_NAME),
                       os.path.join(mei, DEFAULT_SO_NAME)]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def _load_elf_segments(path: str):
    data = open(path, "rb").read()
    if data[:4] != b"\x7fELF":
        raise ValueError(f"{path} is not an ELF file")
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    segs = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, off)[0] != 1:  # PT_LOAD
            continue
        p_offset = struct.unpack_from("<Q", data, off + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        p_memsz = struct.unpack_from("<Q", data, off + 0x28)[0]
        segs.append((p_vaddr, p_offset, p_filesz, p_memsz))
    return data, segs


class AuthEmulator:
    """Runs the real ``function_E1test`` (and ``parse_fw_info``) from the .so."""

    STACK = 0x70000000
    HEAP = 0x60000000
    SCRATCH = 0x50000000
    TLS = 0x7F000000
    RET = 0x40000000

    def __init__(self, so_path: str | None = None):
        if not _HAVE_UNICORN:
            raise RuntimeError(
                "The 'unicorn' package is required for OTA auth. "
                "Install it: pip install unicorn"
            )
        self.so_path = so_path or _default_so_path()
        if not os.path.exists(self.so_path):
            raise FileNotFoundError(
                f"Auth library not found at {self.so_path}. Extract it from the "
                "AnyTone OTA APK: python -m bt_ota.extract_auth_lib <OTA.apk>"
            )
        self.uc = uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        data, segs = _load_elf_segments(self.so_path)
        mapped: list[tuple[int, int]] = []
        for vaddr, off, filesz, memsz in segs:
            base = vaddr & ~0xFFF
            end = (vaddr + memsz + 0xFFF) & ~0xFFF
            if not any(b <= base < b + s for b, s in mapped):
                uc.mem_map(base, end - base)
                mapped.append((base, end - base))
            uc.mem_write(vaddr, data[off:off + filesz])
        uc.mem_map(self.STACK, 0x100000)
        uc.mem_map(self.HEAP, 0x100000)
        self._heap_ptr = self.HEAP
        uc.mem_map(self.SCRATCH, 0x10000)
        uc.mem_map(self.TLS, 0x1000)
        uc.mem_write(self.TLS + 0x28, b"\xde\xad\xbe\xef\xde\xad\xbe\xef")  # stack canary
        uc.reg_write(UC_ARM64_REG_TPIDR_EL0, self.TLS)
        uc.mem_map(self.RET, 0x1000)
        uc.hook_add(UC_HOOK_CODE, self._hook_code)

    # -- libc / PLT shims ----------------------------------------------------
    def _hook_code(self, uc, address, size, user):
        if address not in _PLT:
            return
        name = _PLT[address]
        if name == "decrypt":                      # internal fn via PLT: jump to body
            uc.reg_write(UC_ARM64_REG_PC, REAL_DECRYPT)
            return
        if name == "malloc":
            n = uc.reg_read(UC_ARM64_REG_X0)
            p = self._heap_ptr
            self._heap_ptr += (n + 15) & ~15
            uc.reg_write(UC_ARM64_REG_X0, p)
        elif name == "memcpy":
            dst = uc.reg_read(UC_ARM64_REG_X0)
            src = uc.reg_read(UC_ARM64_REG_X1)
            n = uc.reg_read(UC_ARM64_REG_X2)
            if n:
                uc.mem_write(dst, bytes(uc.mem_read(src, n)))
            uc.reg_write(UC_ARM64_REG_X0, dst)
        elif name == "memcmp":
            a = uc.reg_read(UC_ARM64_REG_X0)
            b = uc.reg_read(UC_ARM64_REG_X1)
            n = uc.reg_read(UC_ARM64_REG_X2)
            da = bytes(uc.mem_read(a, n)) if n else b""
            db = bytes(uc.mem_read(b, n)) if n else b""
            uc.reg_write(UC_ARM64_REG_X0, 0 if da == db else (1 if da > db else (2**64 - 1)))
        elif name == "__strlen_chk":
            s = uc.reg_read(UC_ARM64_REG_X0)
            n = 0
            while n < 0x100000 and uc.mem_read(s + n, 1)[0] != 0:
                n += 1
            uc.reg_write(UC_ARM64_REG_X0, n)
        elif name == "free":
            pass
        elif name == "__stack_chk_fail":
            uc.emu_stop()
            raise RuntimeError("stack check failed during emulation")
        uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    def _call(self, addr, x0, x1, x2, x3):
        uc = self.uc
        uc.reg_write(UC_ARM64_REG_SP, self.STACK + 0x80000)
        uc.reg_write(UC_ARM64_REG_LR, self.RET)
        uc.reg_write(UC_ARM64_REG_X0, x0)
        uc.reg_write(UC_ARM64_REG_X1, x1)
        uc.reg_write(UC_ARM64_REG_X2, x2)
        uc.reg_write(UC_ARM64_REG_X3, x3)
        uc.emu_start(addr, self.RET)

    # -- public API ----------------------------------------------------------
    def get_encrypted_auth_data(self, msg17: bytes) -> bytes:
        """Port of native ``getEncryptedAuthData``.

        out[0] = 0x01; out[1:17] = E1test(addrConst@0x55b0, msg[1:17], keyConst@0x55b6).
        """
        if len(msg17) != 17:
            raise ValueError("auth message must be 17 bytes")
        in_addr = self.SCRATCH
        out_addr = self.SCRATCH + 0x100
        self.uc.mem_write(in_addr, bytes(msg17))
        self.uc.mem_write(out_addr, b"\x00" * 17)
        self._call(FUNCTION_E1TEST, 0x55b0, in_addr + 1, 0x55b6, out_addr + 1)
        out = bytearray(self.uc.mem_read(out_addr, 17))
        out[0] = 1
        return bytes(out)

    def validate_ufw(self, ufw: bytes) -> int:
        """Run native ``parse_fw_info(buf, len, out6, 6)``.

        Returns the native code. -1 = bad header CRC (corrupt), -2 = declared size
        exceeds file (truncated). Any *other* value (incl. -3..-6/-100/0) means the
        64-byte header CRC and length checks passed - the file is a structurally
        valid JLUFW container. (The final go/no-go is the device's own E2 inquire.)
        """
        data = 0x20000000
        size = (len(ufw) + 0xFFF) & ~0xFFF
        try:
            self.uc.mem_map(data, size)
        except UcError:
            self.uc.mem_unmap(data, size)
            self.uc.mem_map(data, size)
        self.uc.mem_write(data, ufw)
        out = self.SCRATCH + 0x2000
        self.uc.mem_write(out, b"\x00" * 16)
        try:
            self._call(PARSE_FW_INFO, data, len(ufw), out, 6)
            ret = self.uc.reg_read(UC_ARM64_REG_X0) & 0xFFFFFFFF
        finally:
            self.uc.mem_unmap(data, size)
        return ret - (1 << 32) if ret >= (1 << 31) else ret


class RcspAuthSession:
    """Reactive port of ``RcspAuth.handleAuthData`` - the mutual handshake.

    Feed it the host's random nonce (``initial_message()``), write that to the
    write characteristic, then hand every inbound *auth-shaped* notification to
    :meth:`handle`. Each call returns the bytes to write back (or ``None``), and
    :attr:`authenticated` flips True on the ``02 'pass'`` confirmation.
    """

    def __init__(self, emu: AuthEmulator, nonce16: bytes | None = None):
        self.emu = emu
        if nonce16 is None:
            nonce16 = os.urandom(16)
        if len(nonce16) != 16:
            raise ValueError("nonce must be 16 bytes")
        self.host_nonce = bytes([0x00]) + nonce16   # type byte 0x00 + 16 random
        self._progress = False
        self.authenticated = False

    @staticmethod
    def is_auth_data(data: bytes) -> bool:
        """Mirror RcspAuth.isValidAuthData: 5-byte [0]==2, or 17-byte [0] in {0,1}."""
        if len(data) == 5 and data[0] == 2:
            return True
        if len(data) == 17 and data[0] in (0, 1):
            return True
        return False

    def initial_message(self) -> bytes:
        return self.host_nonce

    def handle(self, data: bytes) -> bytes | None:
        if self.authenticated or not self.is_auth_data(data):
            return None
        reply: bytes | None = None
        if self._progress:
            if len(data) == 17 and data[0] == 0:            # device's own challenge
                reply = self.emu.get_encrypted_auth_data(bytes(data))
            elif bytes(data) == AUTH_OK:                    # device confirmed us
                self.authenticated = True
                return None
            else:
                return None
        else:
            if not (len(data) == 17 and data[0] == 1):      # device's response to us
                return None
            expected = self.emu.get_encrypted_auth_data(self.host_nonce)
            if expected != bytes(data):
                raise RuntimeError("device auth response mismatch (wrong key?)")
            reply = AUTH_OK
        self._progress = True
        return reply
