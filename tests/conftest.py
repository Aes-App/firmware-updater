import os

os.environ.setdefault("AESAPP_NO_UPDATE_CHECK", "1")

import pytest


@pytest.fixture(autouse=True)
def _no_firmware_catalog_from_the_network(monkeypatch):
    try:
        from radio_fw import gui_tab
    except Exception:
        return
    monkeypatch.setattr(gui_tab.RadioBoardsTab, "_catalog_worker",
                        lambda self: self._post("catalog_err", "tests do not reach the AesApp server"))
