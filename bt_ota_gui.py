"""PyInstaller entry point for the AesApp Radio Updater GUI.

An aesapp:// link on the command line (Windows hands the registered scheme's
URL as the first argument) opens the Digital Contact Refresh tab with it.
"""
import sys

from bt_ota.gui import launch_url_from_argv, main

if __name__ == "__main__":
    main(url=launch_url_from_argv(sys.argv[1:]))
