"""Start Cerberus: launches the API server in the background, then opens the desktop window.

    python main.py            normal start
    python main.py --lan      also let other devices on your Wi-Fi use it (they need your API token)
    python main.py --smoke    open the window, visit every tab, save screenshots to data/, then quit (for testing)
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

import requests


def server_running(port: int) -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/ping", timeout=1.5).json().get("app") == "cerberus"
    except (requests.RequestException, ValueError):
        return False


def main() -> int:
    if sys.stdout is None:                                  # a windowed .exe has no console; uvicorn's logging needs a stream
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    parser = argparse.ArgumentParser(description="Cerberus desktop app")
    parser.add_argument("--lan", action="store_true", help="listen on your home network too")
    parser.add_argument("--smoke", action="store_true", help="self-test: visit every tab, save screenshots, quit")
    args = parser.parse_args()

    import uvicorn

    from cerberus.config import DATA_DIR, DEFAULT_PORT, load_settings, update_settings
    from cerberus.gui.app import CerberusApp
    from cerberus.server import create_app

    from cerberus import backup
    backup.startup_housekeeping()
    settings = load_settings()
    port = settings["server"]["port"] or DEFAULT_PORT
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = None
    server_thread = None
    if server_running(port):
        print(f"A Cerberus server is already running on port {port}; connecting to it.")
    else:
        update_settings({"server": {"host": host, "port": port, "lan": args.lan}})
        app = create_app()
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        server_thread = threading.Thread(target=server.run, daemon=True, name="cerberus-server")
        server_thread.start()
        if args.lan:
            print(f"Listening on the network, port {port}. API token for other devices: {settings['api_token']}")

    def stop_server() -> None:
        if server:
            server.should_exit = True
        if server_thread:
            server_thread.join(timeout=10)

    gui = CerberusApp(f"http://127.0.0.1:{port}", on_close=stop_server)
    if args.smoke:
        def when_ready() -> None:
            if gui.cfg:
                smoke(gui, DATA_DIR)
            else:
                gui.after(500, when_ready)
        gui.after(2500, when_ready)
    gui.mainloop()
    return 0


def smoke(gui, out_dir) -> None:
    """Visit each tab and save a screenshot of the window so the look can be checked without a human."""
    from PIL import ImageGrab

    from cerberus.gui.app import TAB_NAMES
    steps = [(name, 1600 * i) for i, name in enumerate(TAB_NAMES)]

    def shoot(name: str) -> None:
        gui.tabs.set(name)
        gui._tab_changed()
        gui.update()
        gui.after(900, lambda: grab(name))

    def grab(name: str) -> None:
        gui.attributes("-topmost", True)
        gui.lift()
        gui.update()
        x, y, w, h = gui.winfo_rootx(), gui.winfo_rooty(), gui.winfo_width(), gui.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out_dir / f"smoke_{name.replace(' ', '_').lower()}.png")
        print("saved", name)

    for name, delay in steps:
        gui.after(delay + 100, lambda n=name: shoot(n))
    gui.after(1600 * len(steps) + 1500, gui._close)


if __name__ == "__main__":
    sys.exit(main())
