"""Compile picked vendor files into the exact serial wire data + manifest.

The desktop counterpart of the AesApp web CPS's server-side firmware compiler: it
runs the SAME vendored precompilers (radio_fw/vendor/fwupd_*) the server runs, so
the desktop tab validates a package exactly as hard as the web CPS does before a
byte reaches the radio. None of the four update protocols verifies, CRCs or reads
anything back, so a wrong artifact is only discovered when the radio does not
boot — the precompilers hard-fail on every structural deviation, and this module
surfaces their diagnosis VERBATIM (the message names the exact file that is
wrong).

compile_files(kind, paths) -> CompileResult. Raises CompileError with a
human-readable, display-ready message on any rejection (a wrong/missing/extra
file, or a precompiler refusal).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import spec
from .vendor import fwupd_cps, fwupd_nr, fwupd_sct


class CompileError(Exception):
    """A rejected package. `str(self)` is operator-facing and complete."""


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
    """Picked files -> {ext: path}, by extension. Refuses rather than guesses on
    a missing required file, a duplicate extension, or a file the kind does not
    take — picking "the first .CDD" out of an ambiguous set is exactly how a
    radio gets flashed with the wrong image, and nothing downstream would catch
    it."""
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


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise CompileError('Cannot read "' + os.path.basename(path) + '": ' + str(e))


def compile_files(kind: str, paths: list[str]) -> CompileResult:
    """Compile one component from picked vendor files. Raises CompileError with
    a verbatim, display-ready message on any rejection."""
    if kind not in spec.KINDS:
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
            artifact = ufw   # the artifact IS the .ufw, served verbatim
        elif kind in (spec.KIND_ICON, spec.KIND_FW):
            cdd = _read(files["cdd"])
            cdi = _read(files["cdi"])
            spi = _read(files["spi"]) if "spi" in files else None
            artifact, manifest = fwupd_cps.compile_update(kind, cdd, cdi, spi)
        else:  # pragma: no cover - guarded above
            raise CompileError('Unknown update kind "' + str(kind) + '".')
    except (fwupd_cps.UpdateFileError, fwupd_nr.UfwError, fwupd_sct.SctHexError) as e:
        # The precompiler's own words, unedited — they name the violated invariant
        # and are the whole diagnosis.
        raise CompileError(str(e))
    except ValueError as e:
        # Any other stdlib ValueError from parsing (defensive; keep it verbatim).
        raise CompileError(str(e))

    return CompileResult(kind=kind, artifact=bytes(artifact), manifest=manifest, source_names=source_names)
