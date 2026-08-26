"""Pure-Python JieLi RCSP auth (function_E1test), no unicorn / no capstone.

The AnyTone OTA app authenticates to the radio's JieLi BT module with a keyed
transform ``function_E1test`` from ``libjl_ota_auth.so`` (a SAFER+ cipher with a
key baked into the .so). We previously ran that native function under the Unicorn
CPU emulator, but Unicorn's JIT/memory setup access-violates inside a PyInstaller
frozen app on hardened Windows (fine in a venv). This module instead executes the
two small *scalar* AArch64 functions (``function_E1test`` @0x1364 and the SAFER+
round ``sub_1b6c`` @0x1b6c) with a tiny interpreter over a pre-decoded instruction
table (``_jl_itab``), reading the .so's own tables/keys. The NEON key schedule
(``sub_1868``) is reimplemented directly below. Byte-exact vs the emulator over
>20k random vectors.
"""
from __future__ import annotations
import struct
from ._jl_itab import ITAB

M64 = (1 << 64) - 1
M32 = (1 << 32) - 1
_ROL3 = lambda x: ((x << 3) | (x >> 5)) & 0xFF


def _segments(data: bytes):
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    segs = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, o)[0] != 1:  # PT_LOAD
            continue
        segs.append((struct.unpack_from("<Q", data, o + 16)[0],   # vaddr
                     struct.unpack_from("<Q", data, o + 8)[0],     # offset
                     struct.unpack_from("<Q", data, o + 32)[0]))   # filesz
    return segs


class _E1:
    """Executes function_E1test from the given .so image, purely in Python."""

    STACK = 0x70000000
    HEAP = 0x60000000
    IN = 0x50000000
    OUT = 0x50000100
    TLS = 0x7F000000
    RET = 0x40000000
    BIAS_VA = 0xB27  # SAFER+ key-schedule bias table

    def __init__(self, so_data: bytes):
        self.data = so_data
        self.segs = _segments(so_data)
        self._bias0 = self._f2o(self.BIAS_VA)

    def _f2o(self, va):
        for va0, off, fsz in self.segs:
            if va0 <= va < va0 + fsz:
                return off + (va - va0)
        return None

    def _rom(self, a):
        for va0, off, fsz in self.segs:
            if va0 <= a < va0 + fsz:
                return self.data[off + (a - va0)]
        return 0

    # SAFER+ key schedule (native sub_1868), verified byte-exact.
    def _key_schedule(self, key16):
        data = self.data
        b0 = self._bias0
        par = 0
        for b in key16:
            par ^= b
        reg = list(key16) + [par]
        out = bytearray(key16)
        for r in range(16):
            reg = [_ROL3(x) for x in reg]
            for j in range(16):
                out.append((data[b0 + r * 16 - j] + reg[(r + 1 + j) % 17]) & 0xFF)
        return bytes(out)

    def transform(self, msg17: bytes) -> bytes:
        if len(msg17) != 17:
            raise ValueError("auth message must be 17 bytes")
        ram = {}  # writable RAM (stack/heap/scratch); ROM read-through otherwise
        for i, b in enumerate(b"\xde\xad\xbe\xef\xde\xad\xbe\xef"):
            ram[self.TLS + 0x28 + i] = b
        for i, b in enumerate(msg17):
            ram[self.IN + i] = b
        for i in range(17):
            ram[self.OUT + i] = 0
        self._run(0x1364, [0x55B0, self.IN + 1, 0x55B6, self.OUT + 1], ram)
        out = bytearray(ram.get(self.OUT + i, 0) for i in range(17))
        out[0] = 1
        return bytes(out)

    def _run(self, entry, args, ram):
        data = self.data
        R = [0] * 33
        R[31] = self.STACK + 0x80000
        for i, a in enumerate(args):
            R[i] = a & M64
        R[30] = self.RET
        pc = entry
        Z = C = 0
        heap = self.HEAP
        rom = self._rom

        def rd8(a):
            v = ram.get(a)
            return v if v is not None else rom(a)

        def gv(op):
            t = op[0]
            if t == "r":
                _, ri, w32, sh, ux = op
                v = R[ri]
                if w32:
                    v &= M32
                if ux:
                    v &= M32
                if sh:
                    v <<= sh
                return v
            return op[1]  # imm

        def maddr(op):
            _, bi, ii, disp, ish = op
            a = (R[bi] if bi >= 0 else 0)
            if ii >= 0:
                a += R[ii] << ish
            return (a + disp) & M64

        while pc != self.RET:
            m, cc, ops = ITAB[pc]
            npc = pc + 4
            if m == "bl":
                t = ops[0][1]
                if t == 0x1868:
                    key = bytes(rd8(R[0] + i) for i in range(16))
                    ks = self._key_schedule(key)
                    dst = R[1]
                    for i, b in enumerate(ks):
                        ram[dst + i] = b
                elif t == 0x1B6C:
                    R[30] = npc
                    pc = 0x1B6C
                    continue
                elif t == 0x3220:  # malloc
                    n = R[0]
                    R[0] = heap
                    heap = (heap + n + 15) & ~15
                # 0x3230 free: no-op
                pc = npc
                continue
            if m == "ret":
                pc = R[30] & M64
                continue
            if m == "b":
                pc = ops[0][1]
                continue
            if m == "b.eq":
                pc = ops[0][1] if Z else npc
                continue
            if m == "b.ne":
                pc = ops[0][1] if not Z else npc
                continue

            d0 = ops[0]
            if d0[0] == "r":
                dri, dw32 = d0[1], d0[2]

            def setd(v):
                if dw32:
                    v &= M32
                if dri < 31:
                    R[dri] = v & M64
                elif dri == 31:
                    R[31] = v & M64

            if m == "ldrb" or m == "ldurb":
                setd(rd8(maddr(ops[1])))
            elif m == "ldr" or m == "ldur":
                a = maddr(ops[1])
                w = 4 if dw32 else 8
                v = 0
                for i in range(w):
                    v |= rd8(a + i) << (8 * i)
                setd(v)
            elif m == "strb" or m == "sturb":
                ram[maddr(ops[1])] = gv(d0) & 0xFF
            elif m == "str" or m == "stur":
                a = maddr(ops[1])
                v = gv(d0)
                for i in range(4 if d0[2] else 8):
                    ram[a + i] = (v >> (8 * i)) & 0xFF
            elif m == "stp":
                a = maddr(ops[2])
                w = 4 if ops[0][2] else 8
                for k, op in enumerate((ops[0], ops[1])):
                    v = gv(op)
                    for i in range(w):
                        ram[a + k * w + i] = (v >> (8 * i)) & 0xFF
            elif m == "ldp":
                a = maddr(ops[2])
                w = 4 if ops[0][2] else 8
                for k, op in enumerate((ops[0], ops[1])):
                    v = 0
                    for i in range(w):
                        v |= rd8(a + k * w + i) << (8 * i)
                    ri = op[1]
                    if ri < 31:
                        R[ri] = v
            elif m == "mov":
                setd(gv(ops[1]))
            elif m == "mrs":
                setd(self.TLS)
            elif m == "adrp":
                setd(ops[1][1] & M64)
            elif m == "add":
                setd(gv(ops[1]) + gv(ops[2]))
            elif m == "sub":
                setd(gv(ops[1]) - gv(ops[2]))
            elif m == "eor":
                setd(gv(ops[1]) ^ gv(ops[2]))
            elif m == "and":
                setd(gv(ops[1]) & gv(ops[2]))
            elif m == "orr":
                setd(gv(ops[1]) | gv(ops[2]))
            elif m == "ubfx":
                setd((gv(ops[1]) >> ops[2][1]) & ((1 << ops[3][1]) - 1))
            elif m == "cmp" or m == "tst":
                w32 = ops[0][2]
                mask = M32 if w32 else M64
                a = gv(ops[0]) & mask
                b = gv(ops[1]) & mask
                if m == "tst":
                    Z = 1 if (a & b) == 0 else 0
                    C = 0
                else:
                    res = (a - b) & mask
                    Z = 1 if res == 0 else 0
                    C = 1 if a >= b else 0
            elif m == "csel" or m == "csinc":
                cond = ((cc == "eq" and Z) or (cc == "ne" and not Z) or
                        (cc == "hi" and (C and not Z)) or (cc == "lo" and not C) or
                        (cc == "hs" and C) or (cc == "ls" and (not C or Z)))
                setd(gv(ops[1]) if cond else gv(ops[2]) + (0 if m == "csel" else 1))
            else:
                raise NotImplementedError("%#x: %s" % (pc, m))
            pc = npc


def e1test_transform(so_data: bytes, msg17: bytes) -> bytes:
    """out[0]=0x01; out[1:17]=function_E1test(0x55b0, msg17[1:17], 0x55b6). Pure Python."""
    return _E1(so_data).transform(msg17)
