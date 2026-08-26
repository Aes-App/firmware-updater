"""Extract libjl_ota_auth.so (arm64-v8a) from an AnyTone OTA APK.

The auth crypto is a fixed algorithm + fixed key baked into this native library.
We run it under emulation (see jl_auth.py) rather than shipping AnyTone's binary
in the repo. Run this once against the OTA APK you already have:

    python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk"
"""
from __future__ import annotations

import os
import sys
import zipfile

MEMBER_ARM64 = "lib/arm64-v8a/libjl_ota_auth.so"
MEMBER_ARM32 = "lib/armeabi-v7a/libjl_ota_auth.so"


def extract(apk_path: str, dest_dir: str | None = None) -> str:
    dest_dir = dest_dir or os.path.dirname(os.path.abspath(__file__))
    with zipfile.ZipFile(apk_path) as z:
        names = set(z.namelist())
        member = MEMBER_ARM64 if MEMBER_ARM64 in names else None
        if member is None:
            raise FileNotFoundError(
                f"{MEMBER_ARM64} not found in {apk_path}. "
                f"(arm64 build required; armeabi-v7a present: {MEMBER_ARM32 in names})"
            )
        data = z.read(member)
    out = os.path.join(dest_dir, "libjl_ota_auth.so")
    with open(out, "wb") as f:
        f.write(data)
    return out


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    out = extract(argv[0], argv[1] if len(argv) > 1 else None)
    print(f"wrote {out} ({os.path.getsize(out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
