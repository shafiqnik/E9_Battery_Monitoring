import os
import sys

from BatteryGraph import run

raw = sys.argv[1] if len(sys.argv) > 1 else "30AE7BE844CF"
ids = raw.upper().replace(" ", "").replace(":", "").replace("'", "").split(",")
print("you entered =", ids)
open_browser = sys.platform == "win32" or bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
)
run(ids, open_browser=open_browser)
