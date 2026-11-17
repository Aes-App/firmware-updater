from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from radio_fw import changelog, layout, webcps

DIALOG = (468, 474)


def test_a_dialog_goes_over_its_window_on_any_display():
    assert layout.centred_over((600, 228, 960, 820), DIALOG) == (846, 318)
    assert layout.centred_over((-1800, 228, 960, 820), DIALOG) == (-1554, 318)
    assert layout.centred_over((100, -1200, 960, 820), DIALOG) == (346, -1110)
    assert layout.centred_over((3000, 228, 960, 820), DIALOG) == (3246, 318)


def test_the_gap_above_shrinks_to_keep_the_dialog_inside_its_window():
    assert layout.centred_over((0, 100, 960, 600), DIALOG) == (246, 163)
    assert layout.centred_over((0, 100, 960, 400), DIALOG) == (246, 100), \
        "a window shorter than the dialog: their tops line up"
    assert layout.centred_over((0, 100, 400, 820), DIALOG) == (-34, 190), \
        "a narrower window: the dialog overhangs both sides alike"


def _frames():
    import ctypes
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    vp = ctypes.c_void_p

    class Rect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                    ("width", ctypes.c_double), ("height", ctypes.c_double)]

    cg.CGWindowListCopyWindowInfo.restype = vp
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [vp, ctypes.POINTER(Rect)]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [vp]
    cf.CFArrayGetValueAtIndex.restype = vp
    cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
    cf.CFDictionaryGetValue.restype = vp
    cf.CFDictionaryGetValue.argtypes = [vp, vp]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
    cf.CFRelease.argtypes = [vp]

    def field(info, key):
        return cf.CFDictionaryGetValue(info, vp.in_dll(cg, key))

    def number(info, key):
        value, out = field(info, key), ctypes.c_int64()
        return out.value if value and cf.CFNumberGetValue(value, 4, ctypes.byref(out)) else None

    frames = []
    windows = cg.CGWindowListCopyWindowInfo(1, 0)
    try:
        for i in range(cf.CFArrayGetCount(windows)):
            info = cf.CFArrayGetValueAtIndex(windows, i)
            if number(info, "kCGWindowOwnerPID") == os.getpid() and number(info, "kCGWindowLayer") == 0:
                r = Rect()
                if cg.CGRectMakeWithDictionaryRepresentation(field(info, "kCGWindowBounds"), ctypes.byref(r)):
                    frames.append((round(r.x), round(r.y), round(r.width), round(r.height)))
    finally:
        cf.CFRelease(windows)
    return frames


def _dialog_spots(root, seconds=0.5):
    main_width = root.winfo_width()
    spots = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        for x, y, width, _height in _frames():
            if width != main_width and (not spots or spots[-1] != (x, y)):
                spots.append((x, y))
        time.sleep(0.01)
    return spots


@pytest.mark.skipif(sys.platform != "darwin", reason="the window server asked is macOS's")
def test_macos_shows_the_dialogs_over_the_window_from_the_first_frame():
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    try:
        root.attributes("-alpha", 0.0)
        root.geometry("900x700+160+140")
        for _ in range(6):
            root.update()
        main = (root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height())

        backup = webcps.BackupDialog(root, "D890UV", webcps.Coverage(("1.04", "1.05"), True), "1.05")
        backup.win.attributes("-alpha", 0.0)
        want = layout.centred_over(main, (backup.win.winfo_reqwidth(), backup.win.winfo_reqheight()))
        assert _dialog_spots(root) == [want], f"the backup question should only ever be at {want}"
        backup.close()

        log = changelog.ChangelogDialog(root, "D890UV", [("1.05", "Released 2025.", "Main firmware")],
                                        "1.05", lambda _label: None)
        log.win.attributes("-alpha", 0.0)
        spots = _dialog_spots(root)
        want = layout.centred_over(main, (log.win.winfo_width(), log.win.winfo_height()), gap=60)
        assert spots == [want], f"the changelog should only ever be at {want}"
        log.close()
    finally:
        root.destroy()
