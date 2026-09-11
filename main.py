import os
import sys


def main():
    print("Starting E9 Battery Monitoring...")
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    from BatteryGraph import run
    from credentials import load_config, parse_ids

    cfg = load_config()
    raw = sys.argv[1] if len(sys.argv) > 1 else cfg.esn
    ids = parse_ids(raw)
    print("you entered =", ids)
    open_browser = sys.platform == "win32" or bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )
    run(ids, open_browser=open_browser)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
