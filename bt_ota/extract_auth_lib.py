"""Extract libjl_ota_auth.so (arm64-v8a) from the JieLi OTA SDK's AAR, or from
an Android OTA app's APK.

The auth crypto is a fixed algorithm + fixed key baked into this native library.
We run it under interpretation (see jl_auth.py) rather than committing the binary
to the repo. The library is JieLi's, Apache-2.0; the build this tool is verified
against is the one in jl_bt_ota_V1.9.2-release.aar of
https://github.com/Jieli-Tech/Android-JL_OTA . Run this once:

    python -m bt_ota.extract_auth_lib jl_bt_ota_V1.9.2-release.aar
    python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk"
"""
from __future__ import annotations

import hashlib
import os
import sys
import zipfile

MEMBERS_ARM64 = ("jni/arm64-v8a/libjl_ota_auth.so", "lib/arm64-v8a/libjl_ota_auth.so")
MEMBERS_ARM32 = ("jni/armeabi-v7a/libjl_ota_auth.so", "lib/armeabi-v7a/libjl_ota_auth.so")


def extract(archive_path: str, dest_dir: str | None = None) -> str:
    dest_dir = dest_dir or os.path.dirname(os.path.abspath(__file__))
    with zipfile.ZipFile(archive_path) as z:
        names = set(z.namelist())
        member = next((m for m in MEMBERS_ARM64 if m in names), None)
        if member is None:
            raise FileNotFoundError(
                f"no arm64-v8a libjl_ota_auth.so in {archive_path} "
                f"(armeabi-v7a present: {any(m in names for m in MEMBERS_ARM32)}; "
                "the arm64 build is the one this tool interprets)"
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
    data = open(out, "rb").read()
    print(f"wrote {out} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    from .jl_auth import KNOWN_SO_SHA256
    if hashlib.sha256(data).hexdigest() != KNOWN_SO_SHA256:
        print("WARNING: this is not the build the updater is verified against; "
              "the app will refuse it. Use jl_bt_ota_V1.9.2-release.aar.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
