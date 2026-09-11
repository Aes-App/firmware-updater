"""Suite-wide settings.

The one thing here is a hard stop on the startup update check. bt_ota.gui asks
GitHub for the newest release when a window opens, and a test suite that reaches
the network is a test suite that fails on a train: slow, flaky, and dependent on
a rate limit shared with everyone else behind the same address. Tests that want
to exercise the check call bt_ota.update_check.check() directly with their own
opener, which is what it takes one for.
"""
import os

os.environ.setdefault("AESAPP_NO_UPDATE_CHECK", "1")
