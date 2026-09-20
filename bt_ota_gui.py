import sys

from bt_ota.gui import launch_url_from_argv, main

if __name__ == "__main__":
    main(url=launch_url_from_argv(sys.argv[1:]))
