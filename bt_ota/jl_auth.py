from __future__ import annotations

import os
import struct
import sys


def _prefer_bundled_unicorn() -> None:
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
except ImportError:
    _HAVE_UNICORN = False

from ._jl_e1 import _E1


FUNCTION_E1TEST = 0x1364
REAL_DECRYPT = 0x2250
PARSE_FW_INFO = 0x2384
_PLT = {
    0x3220: "malloc", 0x3230: "free", 0x3240: "__stack_chk_fail",
    0x3260: "decrypt", 0x3280: "__strlen_chk", 0x3290: "memcmp", 0x32a0: "memcpy",
}

DEFAULT_SO_NAME = "libjl_ota_auth.so"

AUTH_OK = bytes([2, 0x70, 0x61, 0x73, 0x73])


def _default_so_path() -> str:
    env = os.environ.get("JL_OTA_AUTH_SO")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, DEFAULT_SO_NAME)]
    mei = getattr(sys, "_MEIPASS", None)
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
        if struct.unpack_from("<I", data, off)[0] != 1:
            continue
        p_offset = struct.unpack_from("<Q", data, off + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        p_memsz = struct.unpack_from("<Q", data, off + 0x28)[0]
        segs.append((p_vaddr, p_offset, p_filesz, p_memsz))
    return data, segs


class AuthEmulator:

    STACK = 0x70000000
    HEAP = 0x60000000
    SCRATCH = 0x50000000
    TLS = 0x7F000000
    RET = 0x40000000

    def __init__(self, so_path: str | None = None):
        self.so_path = so_path or _default_so_path()
        if not os.path.exists(self.so_path):
            raise FileNotFoundError(
                f"Auth library not found at {self.so_path}. Extract it from the "
                "AnyTone OTA APK: python -m bt_ota.extract_auth_lib <OTA.apk>"
            )
        self._so_data = open(self.so_path, "rb").read()
        if self._so_data[:4] != b"\x7fELF":
            raise ValueError(f"{self.so_path} is not an ELF file")
        self._e1 = _E1(self._so_data)
        self.uc = None

    def _ensure_uc(self):
        if self.uc is not None:
            return
        if not _HAVE_UNICORN:
            raise RuntimeError(
                "validate_ufw needs the optional 'unicorn' package; the auth and "
                "upgrade paths do not. Install it: pip install unicorn")
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
        uc.mem_write(self.TLS + 0x28, b"\xde\xad\xbe\xef\xde\xad\xbe\xef")
        uc.reg_write(UC_ARM64_REG_TPIDR_EL0, self.TLS)
        uc.mem_map(self.RET, 0x1000)
        uc.hook_add(UC_HOOK_CODE, self._hook_code)

    def _hook_code(self, uc, address, size, user):
        if address not in _PLT:
            return
        name = _PLT[address]
        if name == "decrypt":
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

    def get_encrypted_auth_data(self, msg17: bytes) -> bytes:
        return self._e1.transform(msg17)

    def validate_ufw(self, ufw: bytes) -> int:
        if not _HAVE_UNICORN:
            return 0
        self._ensure_uc()
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

    def __init__(self, emu: AuthEmulator, nonce16: bytes | None = None):
        self.emu = emu
        if nonce16 is None:
            nonce16 = os.urandom(16)
        if len(nonce16) != 16:
            raise ValueError("nonce must be 16 bytes")
        self.host_nonce = bytes([0x00]) + nonce16
        self._progress = False
        self.authenticated = False

    @staticmethod
    def is_auth_data(data: bytes) -> bool:
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
            if len(data) == 17 and data[0] == 0:
                reply = self.emu.get_encrypted_auth_data(bytes(data))
            elif bytes(data) == AUTH_OK:
                self.authenticated = True
                return None
            else:
                return None
        else:
            if not (len(data) == 17 and data[0] == 1):
                return None
            expected = self.emu.get_encrypted_auth_data(self.host_nonce)
            if expected != bytes(data):
                raise RuntimeError("device auth response mismatch (wrong key?)")
            reply = AUTH_OK
        self._progress = True
        return reply
