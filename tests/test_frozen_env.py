from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("tkinter")
pytest.importorskip("bleak")

from bt_ota import gui

MEI = "/tmp/_MEI12345abc"


@pytest.fixture()
def frozen_linux(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", MEI, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    for k in ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_the_users_own_value_comes_back(frozen_linux):
    frozen_linux.setenv("LD_LIBRARY_PATH", MEI)
    frozen_linux.setenv("LD_LIBRARY_PATH_ORIG", "/opt/sdr/lib")
    gui._restore_child_env()
    assert os.environ["LD_LIBRARY_PATH"] == "/opt/sdr/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in os.environ


def test_with_no_value_of_its_own_the_bundle_is_taken_out(frozen_linux):
    frozen_linux.setenv("LD_LIBRARY_PATH", MEI)
    gui._restore_child_env()
    assert "LD_LIBRARY_PATH" not in os.environ
    frozen_linux.setenv("LD_LIBRARY_PATH", MEI + os.pathsep + "/opt/sdr/lib")
    gui._restore_child_env()
    assert os.environ["LD_LIBRARY_PATH"] == "/opt/sdr/lib"


def test_nothing_changes_outside_a_frozen_linux_build(frozen_linux):
    frozen_linux.setenv("LD_LIBRARY_PATH", MEI)
    frozen_linux.setattr(sys, "platform", "darwin")
    gui._restore_child_env()
    frozen_linux.setattr(sys, "platform", "linux")
    frozen_linux.setattr(sys, "frozen", False)
    gui._restore_child_env()
    assert os.environ["LD_LIBRARY_PATH"] == MEI
