"""Real end-to-end run against your local Ollama (not part of the normal unit tests).

    .venv\\Scripts\\python.exe -m tests.e2e_real [mode] "your message"
"""
import sys
import time

from cerberus.core import Cerberus

from .helpers import Isolated


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 2 else "conversation"
    message = sys.argv[-1] if len(sys.argv) > 1 else "Hello. Who are you?"
    with Isolated() as root:
        core = Cerberus(root / "e2e.db")
        t0 = time.time()
        for ev in core.orchestrator.run(message, mode, {}):
            t = ev["type"]
            if t == "token":
                if ev["kind"] == "content":
                    print(ev["text"], end="", flush=True)
            elif t == "stage_start":
                print(f"\n\n=== [{time.time() - t0:5.1f}s] {ev['title']} ({ev.get('model')}) ===")
            elif t == "final":
                print(f"\n\n##### FINAL ({ev['source']}) #####\n{ev['text']}")
            elif t in ("error", "files", "start"):
                print(f"\n[{t}] {str(ev)[:400]}")
        print(f"\nTotal {time.time() - t0:.1f}s")
        core.db.close()


if __name__ == "__main__":
    main()
