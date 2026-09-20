from __future__ import annotations

import os
from dataclasses import dataclass

from . import spec
from .vendor import fwupd_cps, fwupd_nr, fwupd_sct


class CompileError(Exception):
    pass


@dataclass
class CompileResult:
    kind: str
    artifact: bytes
    manifest: dict
    source_names: list[str]

    @property
    def frames(self) -> int:
        return int(self.manifest.get("frames", 0))

    @property
    def payload_bytes(self) -> int:
        return int(self.manifest.get("payload_bytes", 0))

    @property
    def sha256(self) -> str:
        return str(self.manifest.get("sha256", ""))


def _by_extension(kind: str, paths: list[str]) -> dict[str, str]:
    spc = spec.KINDS[kind]
    allowed = list(spc["required"]) + list(spc["optional"])
    seen: dict[str, str] = {}
    for path in paths:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        name = os.path.basename(path)
        if ext not in allowed:
            raise CompileError(
                '"' + name + '" is not a file the ' + spec.label(kind) + " update takes — it accepts "
                + _ext_list(allowed) + ".")
        if ext in seen:
            raise CompileError(
                "Two ." + ext.upper() + ' files were chosen ("' + os.path.basename(seen[ext]) + '" and "'
                + name + '") — choose exactly one of each.')
        seen[ext] = path
    for ext in spc["required"]:
        if ext not in seen:
            raise CompileError(
                "No ." + ext.upper() + " file was chosen — the " + spec.label(kind) + " update needs "
                + _ext_list(list(spc["required"])) + ".")
    return seen


def _ext_list(exts: list[str]) -> str:
    return ", ".join("." + e.upper() for e in exts)


def _detect_878_gen(spi: bytes | None, fallback: str) -> str:
    if fallback not in ("d878uv", "d878uv2") or not spi:
        return fallback
    if b"D878UV2" in spi:
        return "d878uv2"
    if b"D878UV" in spi:
        return "d878uv"
    return fallback


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise CompileError('Cannot read "' + os.path.basename(path) + '": ' + str(e))


def compile_files(kind: str, paths: list[str], model: str = "d890") -> CompileResult:
    if kind not in spec.KIND_FILESPEC:
        raise CompileError('Unknown update kind "' + str(kind) + '".')
    files = _by_extension(kind, paths)
    source_names = [os.path.basename(p) for p in files.values()]

    try:
        if kind == spec.KIND_SCT:
            artifact, manifest = fwupd_sct.compile_stream(
                fwupd_sct.parse_sct_hex(_read(files["hex"])))
        elif kind == spec.KIND_NR:
            ufw = _read(files["ufw"])
            manifest = fwupd_nr.build_manifest(ufw)
            artifact = ufw
        elif kind in spec.CPS_KINDS:
            cdd = _read(files["cdd"])
            cdi = _read(files["cdi"])
            spi = _read(files["spi"]) if "spi" in files else None
            cps = spec.cps_model(model)
            if kind == spec.KIND_FW:
                cps = _detect_878_gen(spi, cps)
            artifact, manifest = fwupd_cps.compile_update(kind, cdd, cdi, spi, model=cps)
        else:
            raise CompileError('Unknown update kind "' + str(kind) + '".')
    except (fwupd_cps.UpdateFileError, fwupd_nr.UfwError, fwupd_sct.SctHexError) as e:
        raise CompileError(str(e))
    except ValueError as e:
        raise CompileError(str(e))

    return CompileResult(kind=kind, artifact=bytes(artifact), manifest=manifest, source_names=source_names)
