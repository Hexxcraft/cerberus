"""Start the Cerberus server so phones and other computers on your home Wi-Fi can use it.

Run:  python scripts/start_lan_server.py      (or double-click start_lan_server.bat)

What it does
  1. Finds this computer's home-network address(es).
  2. Shows the API token other devices must enter once (it is stored in data/settings.json).
  3. Optionally opens the Windows Firewall for the Private network profile only (needs Administrator).
  4. Starts the server on 0.0.0.0 so the whole network can reach it.

Safety: devices on your network can chat with Cerberus, but they CANNOT change settings, manage agents,
turn on Autonomous Agent Mode, or use mouse/keyboard tools. Only this computer can do those things.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cerberus.config import DEFAULT_PORT, load_settings, update_settings  # noqa: E402
from cerberus.server import create_app, lan_ips  # noqa: E402


def open_firewall(port: int) -> None:
    cmd = ["netsh", "advfirewall", "firewall", "add", "rule", "name=Cerberus LAN", "dir=in", "action=allow",
           "protocol=TCP", f"localport={port}", "profile=private"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Could not run netsh: {exc}")
        return
    if out.returncode == 0:
        print("  Firewall rule added (Private networks only).")
    else:
        print("  Could not add the firewall rule (it needs an Administrator terminal). Details:")
        print("   ", (out.stdout or out.stderr).strip())
        print("  Windows may also pop up its own 'allow access' question the first time - choose Private networks.")


def main() -> None:
    from cerberus import backup
    backup.startup_housekeeping()
    settings = load_settings()
    port = settings["server"]["port"] or DEFAULT_PORT
    ips = lan_ips()
    print("\n  === Cerberus LAN server ===\n")
    if not ips:
        print("  No home-network address was found. Are you connected to Wi-Fi or Ethernet?\n")
    else:
        print("  Open one of these on your phone or another computer:")
        for ip in ips:
            print(f"      http://{ip}:{port}")
    print(f"\n  API token (type it once on the other device):\n      {settings['api_token']}\n")
    if input("  Open the Windows Firewall for port %d on private networks? [y/N] " % port).strip().lower() == "y":
        open_firewall(port)
    update_settings({"server": {"host": "0.0.0.0", "port": port, "lan": True}})
    print("\n  Starting... press Ctrl+C to stop.\n")
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
