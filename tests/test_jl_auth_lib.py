from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bt_ota import jl_auth

_ELF64_HEADER = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)


def test_a_different_build_is_refused_by_name(tmp_path):
    fake = tmp_path / "libjl_ota_auth.so"
    fake.write_bytes(_ELF64_HEADER + b"\0" * 64)
    with pytest.raises(ValueError, match="not the libjl_ota_auth.so build") as e:
        jl_auth.AuthEmulator(str(fake))
    assert "jl_bt_ota_V1.9.2" in str(e.value)


def test_a_non_elf_file_is_refused(tmp_path):
    fake = tmp_path / "libjl_ota_auth.so"
    fake.write_bytes(b"not an elf")
    with pytest.raises(ValueError, match="not an ELF"):
        jl_auth.AuthEmulator(str(fake))


def test_the_pin_matches_the_library_on_this_machine_when_present():
    path = jl_auth._default_so_path()
    if not os.path.exists(path):
        pytest.skip("no libjl_ota_auth.so in this working copy")
    data = open(path, "rb").read()
    assert len(data) == jl_auth.KNOWN_SO_SIZE
    assert hashlib.sha256(data).hexdigest() == jl_auth.KNOWN_SO_SHA256
    jl_auth.AuthEmulator(path)
