"""Manual GUI check with real Ollama: types a message into the app, waits for the answer and saves a screenshot.

    python -m tests.gui_chat conversation "Hello, who are you?"
"""
import sys
import threading
import time

import uvicorn
from PIL import ImageGrab

from cerberus.config import DATA_DIR, DEFAULT_PORT
from cerberus.gui.app import CerberusApp
from cerberus.server import create_app

mode = sys.argv[1] if len(sys.argv) > 1 else "conversation"
message = sys.argv[2] if len(sys.argv) > 2 else "Hello, who are you?"
port = DEFAULT_PORT + 1
server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
gui = CerberusApp(f"http://127.0.0.1:{port}")
started = {"t": time.time(), "sent": False}


def step() -> None:
    if not gui.cfg:
        return gui.after(500, step)
    if not started["sent"]:
        gui.select_mode(mode)
        gui.after(1500, send)
        started["sent"] = True
        return
    gui.after(500, step)


def send() -> None:
    gui.input.insert("1.0", message)
    gui.send()
    started["t"] = time.time()
    gui.after(1000, wait)


def wait() -> None:
    if gui.running and time.time() - started["t"] < 300:
        return gui.after(1000, wait)
    gui.update()
    x, y, w, h = gui.winfo_rootx(), gui.winfo_rooty(), gui.winfo_width(), gui.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(DATA_DIR / "gui_chat.png")
    print("finished in", round(time.time() - started["t"]), "s; text length", len(gui.chat.get("1.0", "end")))
    gui._close()


gui.after(2000, step)
gui.mainloop()
server.should_exit = True
