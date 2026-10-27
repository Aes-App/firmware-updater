from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from radio_fw import compiler, spec
from radio_fw.vendor import fwupd_cps


def test_every_target_has_a_compiler_target_and_instructions():
    for model in spec.MODELS:
        assert spec.model_order(model), model
        for kind in spec.model_order(model):
            assert spec.entry_instructions(model, kind), (model, kind)
            if kind in spec.CPS_KINDS:
                assert kind in fwupd_cps.MODELS[spec.cps_model(model)], (model, kind)


def test_the_d890_photos_are_not_shown_for_other_keypads():
    shared = {spec.image(k) for k in spec.KIND_FILESPEC} | {spec.reset_image("d890")}
    for model in ("d878uv2", "d168", "d578uv2", "d578uv", "bt01"):
        for kind in spec.model_order(model):
            assert spec.step_image(model, kind) not in shared, (model, kind)
        assert spec.reset_image(model) not in shared
    assert spec.step_image("d890", spec.KIND_FW) == spec.image(spec.KIND_FW)
    assert spec.reset_image("d878uv2") == "D878_Reset.png"


def test_every_step_photo_exists_and_every_build_ships_it():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert len(spec.step_photos()) == 20
    for name in sorted(spec.step_photos()):
        assert os.path.exists(os.path.join(root, "radio_fw", "assets", name)), name
    for n in ("bt_ota_gui.spec", "bt_ota_gui_win.spec", "bt_ota_gui_linux.spec"):
        assert 'glob.glob("radio_fw/assets/*.png")' in open(os.path.join(root, n)).read(), n


def test_unconfirmed_entry_is_per_model():
    assert spec.is_unconfirmed("d578uv2", spec.KIND_ICON)
    assert spec.is_unconfirmed("d578uv", spec.KIND_ICON)
    assert not spec.is_unconfirmed("d168", spec.KIND_ICON)
    assert not spec.is_unconfirmed("d890", spec.KIND_ICON)


def test_d578_has_its_own_mcu_reset():
    assert "P2" in spec.mcu_reset("d578uv2")
    assert spec.mcu_reset("d168") == spec.mcu_reset("d890")


def _write_pkg(folder, stem, addr, payload, tag):
    name = b"fw.bin".ljust(0x100, b"\x00")
    cdi = name + struct.pack("<IIII", addr, len(payload), 0, 0x10000) + b"\x00" * 6
    spi = (struct.pack("<IHII", 32, 1, len(payload), 0) + tag.encode().ljust(8) + b"V100"
           + tag.encode().ljust(8) + b" " * 8 + b"V100" + b" " * 4 + b"\x00")
    paths = []
    for ext, data in (("CDD", payload), ("CDI", cdi), ("spi", spi)):
        p = os.path.join(folder, stem + "." + ext)
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    return paths


def test_compile_files_picks_the_d578_mcu_from_the_spi(tmp_path):
    at = _write_pkg(str(tmp_path), "at", 0x08006000, bytes(range(96)), "578UV2A")
    gd = _write_pkg(str(tmp_path), "gd", 0x08006000, bytes(range(96)), "D578UV2")
    assert compiler.compile_files("fw", at, "d578uv2").manifest["ident_reply_prefix_ascii"] == "I578UV2A"
    assert compiler.compile_files("fw", gd, "d578uv2").manifest["ident_reply_prefix_ascii"] == "ID578UV2"
    with pytest.raises(compiler.CompileError, match="needs the package's .spi"):
        compiler.compile_files("fw", at[:2], "d578uv2")


def _write_sct_hex(path, addrs):
    def line(cnt, addr, typ, data=b""):
        body = bytes([cnt, addr >> 8, addr & 0xFF, typ]) + data
        return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()
    lines = []
    for a in addrs:
        lines.append(line(0, a >> 16, 0x04))
        lines.append(line(16, a & 0xFFFF, 0x00, bytes(range(16))))
    lines.append(":00000001FF")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def test_the_radio_picks_the_baseband_ladder(tmp_path):
    h3258 = _write_sct_hex(str(tmp_path / "3258.hex"), [0x000100, 0x018000])
    h3288 = _write_sct_hex(str(tmp_path / "3288.hex"), [0x000100, 0x022000])
    res = compiler.compile_files(spec.KIND_SCT, [h3258], "d578uv2")
    assert res.manifest["kind"] == "sct3258_baseband"
    for model in ("d168", "d890"):
        assert compiler.compile_files(spec.KIND_SCT, [h3288], model).manifest["kind"] == "sct3288_baseband"
        with pytest.raises(compiler.CompileError, match="SCT3288 erase ladder"):
            compiler.compile_files(spec.KIND_SCT, [h3258], model)
    with pytest.raises(compiler.CompileError, match="SCT3258 erase ladder"):
        compiler.compile_files(spec.KIND_SCT, [h3288], "d578uv2")


def test_baseband_label_and_order_per_model():
    assert spec.label(spec.KIND_SCT, "d578uv2") == "SCT3258 Baseband"
    assert spec.label(spec.KIND_SCT, "d168") == spec.label(spec.KIND_SCT) == "SCT3288 Baseband"
    for model in ("d168", "d578uv2"):
        assert spec.KIND_SCT in spec.model_order(model)
    assert spec.sct_profile("d578uv2") == "sct3258"
    assert spec.sct_profile("d890") == spec.sct_profile("d168") == "sct3288"


def test_compile_files_refuses_another_radios_package(tmp_path):
    d168 = _write_pkg(str(tmp_path), "d168", 0x08004000, bytes(range(96)), "D168UV")
    assert compiler.compile_files("fw", d168, "d168").manifest["ident_reply_prefix_ascii"] == "ID168UV"
    for model in ("d878uv2", "d890", "d578uv2"):
        with pytest.raises(compiler.CompileError, match="wrong package for this radio"):
            compiler.compile_files("fw", d168, model)


def test_the_bt01_is_not_a_radio():
    assert spec.model_order("bt01") == [spec.KIND_FW, spec.KIND_ICON]
    assert spec.label(spec.KIND_FW, "bt01") == "Microphone Firmware"
    assert spec.label(spec.KIND_ICON, "bt01") == "Microphone Icons"
    assert "no radio is connected" in spec.entry_instructions("bt01", spec.KIND_ICON)
    assert not spec.has_codeplug("bt01") and spec.mcu_reset("bt01") == ""
    assert all(spec.has_codeplug(m) and spec.mcu_reset(m) for m in spec.MODELS if m != "bt01")
    assert spec.model_order("d578uv2") == [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT, spec.KIND_APRS]


def _downloaded(kind, manifest):
    return compiler.CompileResult(kind=kind, artifact=b"", manifest=manifest, source_names=[])


def test_a_downloaded_baseband_must_be_for_the_radios_chip():
    s3258 = _downloaded(spec.KIND_SCT, {"kind": "sct3258_baseband"})
    s3288 = _downloaded(spec.KIND_SCT, {"kind": "sct3288_baseband"})
    assert compiler.check_downloaded(s3258, "d578uv2") is s3258
    for model in ("d890", "d168"):
        assert compiler.check_downloaded(s3288, model) is s3288
        with pytest.raises(compiler.CompileError, match="SCT3288"):
            compiler.check_downloaded(s3258, model)
    with pytest.raises(compiler.CompileError, match="SCT3258"):
        compiler.check_downloaded(s3288, "d578uv2")


def test_a_downloaded_cps_component_must_be_for_this_radio():
    icon = lambda m: _downloaded(spec.KIND_ICON, {"kind": "icon", "model": m})
    assert compiler.check_downloaded(icon("d168"), "d168")
    for gen in ("d878uv", "d878uv2"):
        assert compiler.check_downloaded(icon(gen), gen)
    with pytest.raises(compiler.CompileError, match="built for \"d878uv\""):
        compiler.check_downloaded(icon("d878uv"), "d878uv2")
    with pytest.raises(compiler.CompileError, match="built for \"d878uv2\""):
        compiler.check_downloaded(icon("d878uv2"), "d168")
    with pytest.raises(compiler.CompileError, match="not the BT01 Mic"):
        compiler.check_downloaded(icon("d578uv2"), "bt01")
    older = _downloaded(spec.KIND_FW, {"kind": "fw"})
    assert compiler.check_downloaded(older, "d890") is older


def test_the_d578uv_on_firmware_1xx():
    assert spec.model_order("d578uv") == [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT]
    assert spec.sct_profile("d578uv") == "sct3258"
    assert spec.mcu_reset("d578uv") == spec.mcu_reset("d578uv2")
    assert spec.is_untested("d578uv")
    assert "not been tested on a real radio" in spec.untested_warning("d578uv")
    assert [m for m in spec.MODELS if spec.is_untested(m)] == ["d578uv"]
    assert spec.model_label("d578uv2") == "D578UV(III) Plus"
    assert spec.model_label("d578uv") == "D578UV(III) G/Pro"


def test_each_d578_button_names_the_radios_it_is_for():
    plus, rest = spec.covers("d578uv2"), spec.covers("d578uv")
    assert "AT-D578UV PLUS" in plus and "AT-D578UVIII PLUS" in plus and "2.xx" in plus
    for name in ("AT-D578UV,", "AT-D578UVIII,", "AT-D578UVG", "AT-D578UVIIIG", "AT-D578UV PRO",
                 "AT-D578UVIII PRO"):
        assert name in rest and name not in plus, name
    assert "1.xx" in rest
    assert [m for m in spec.MODELS if spec.covers(m)] == ["d578uv2", "d578uv"]
    assert spec.server_model_name("d578uv2") == "D578UV(III) Plus"
    assert spec.server_model_name("d578uv") == "D578UV(III) G/Pro"


def test_d578_packages_stay_on_their_own_generation(tmp_path):
    gen1 = _write_pkg(str(tmp_path), "g1", 0x08006000, bytes(range(96)), "D578UVA")
    gen2 = _write_pkg(str(tmp_path), "g2", 0x08006000, bytes(range(96)), "578UV2A")
    assert compiler.compile_files("fw", gen1, "d578uv").manifest["ident_reply_prefix_ascii"] == "ID578UVA"
    with pytest.raises(compiler.CompileError, match="wrong package for this radio"):
        compiler.compile_files("fw", gen1, "d578uv2")
    with pytest.raises(compiler.CompileError, match="wrong package for this radio"):
        compiler.compile_files("fw", gen2, "d578uv")


def test_a_d578_firmware_names_its_mcu_from_the_spi(tmp_path):
    for model, tag, chip in (("d578uv2", "578UV2A", "AT32"), ("d578uv2", "D578UV2", "GD32"),
                             ("d578uv", "D578UVA", "AT32"), ("d578uv", "D578UV", "GD32")):
        pkg = _write_pkg(str(tmp_path), tag, 0x08006000, bytes(range(96)), tag)
        res = compiler.compile_files("fw", pkg, model)
        assert spec.fw_chip(res.manifest) == chip, (model, tag)
        assert chip in spec.usb_chips(model)
    per_mcu = {i for m in fwupd_cps.MODELS.values() for i in (m.get("fw", {}).get("ident") or ())
               if isinstance(m.get("fw", {}).get("ident"), tuple)}
    assert per_mcu == set(spec.FW_IDENT_CHIP)
    assert spec.fw_chip({"ident_reply_prefix_ascii": "ID168UV"}) is None
    assert spec.fw_chip(None) is None


def test_the_wrong_mcus_port_is_refused_both_ways():
    at = {"ident_reply_prefix_ascii": "I578UV2A"}
    gd = {"ident_reply_prefix_ascii": "ID578UV2"}
    refusal = spec.fw_port_mismatch("d578uv2", at, "GD32", "COM5")
    assert "the AT build" in refusal and "needs the GD build" in refusal and "COM5 is a GD32 USB port" in refusal
    assert "will NOT be written" in refusal and "anyway" not in refusal
    refusal = spec.fw_port_mismatch("d578uv2", gd, "AT32", "COM5")
    assert "the GD build" in refusal and "needs the AT build" in refusal and "COM5 is an AT32 USB port" in refusal
    gen1 = {"ident_reply_prefix_ascii": "ID578UVA"}
    assert "needs the GD build" in spec.fw_port_mismatch("d578uv", gen1, "GD32", "COM5")
    assert spec.fw_port_mismatch("d578uv2", at, "AT32", "COM5") == ""
    assert spec.fw_port_mismatch("d578uv2", at, None, "COM5") == ""
    assert spec.fw_port_mismatch("d578uv2", at, "STM32", "COM5") == ""
    assert spec.fw_port_mismatch("d168", {"ident_reply_prefix_ascii": "ID168UV"}, "GD32", "COM5") == ""
    assert spec.fw_port_mismatch("d578uv2", None, "GD32", "COM5") == ""


def test_the_d578s_are_one_button_resolved_by_the_radio():
    assert "d578" in spec.MODEL_ORDER and not {"d578uv2", "d578uv"} & set(spec.MODEL_ORDER)
    assert spec.choice_label("d578") == "D578 Series"
    reached = [m for c in spec.MODEL_ORDER for m in spec.family_members(c)]
    assert sorted(reached) == sorted(spec.MODELS)
    assert spec.identified_model("d578", "D578UV2") == "d578uv2"
    assert spec.identified_model("d578", "D578UV") == "d578uv"
    assert spec.identified_model("d578", "D878UV2") is None
    assert spec.family_of("d578uv") == "d578" and spec.family_of("d890") == "d890"
    assert [spec.variant_label("d578", m) for m in spec.family_members("d578")] == [
        "D578UV (1st Gen)", "D578UV2 (2nd Gen)"]
    assert [spec.chip_label("d578", c) for c in spec.family_chips("d578")] == [
        "AT32 MCU (New)", "GD32 MCU (Old)"]


def test_the_build_each_d578_needs_is_the_compilers():
    for (model, chip), ident in spec.FW_BUILD_IDENT.items():
        assert ident in fwupd_cps.MODELS[spec.cps_model(model)]["fw"]["ident"], (model, ident)
        assert spec.fw_chip_of_ident(ident) == chip
        assert spec.fw_build_mismatch(model, chip, {"ident_reply_prefix_ascii": ident}) == ""
        other = "GD32" if chip == "AT32" else "AT32"
        why = spec.fw_build_mismatch(model, other, {"ident_reply_prefix_ascii": ident})
        assert "needs the " + other[:2] + " build" in why, why
    assert spec.fw_build_mismatch("d578uv2", None, {"ident_reply_prefix_ascii": "I578UV2A"}) == ""


def test_the_d878s_are_one_button_resolved_by_the_radio():
    assert "d878" in spec.MODEL_ORDER and "d878uv2" not in spec.MODEL_ORDER
    assert spec.choice_label("d878") == "D878 Series"
    assert spec.identified_model("d878", "D878UV") == "d878uv"
    assert spec.identified_model("d878", "D878UV2") == "d878uv2"
    assert spec.identified_model("d878", "D578UV2") is None
    assert spec.model_order("d878uv") == [spec.KIND_FW, spec.KIND_ICON]
    assert spec.model_order("d878uv2") == [spec.KIND_FW, spec.KIND_ICON, spec.KIND_APRS]
    assert spec.server_models("d878uv") == ["d878uv"] and spec.server_models("d878uv2") == ["d878uv2"]
    why = spec.fw_model_mismatch("d878", "d878uv2", {"model": "d878uv"})
    assert "D878UV (1st Gen)" in why and "D878UVII (2nd Gen)" in why
    assert spec.fw_model_mismatch("d878", "d878uv2", {"model": "d878uv2"}) == ""
    assert spec.fw_model_mismatch("", "d890", {"model": "d878uv"}) == "", "only a family's members"
