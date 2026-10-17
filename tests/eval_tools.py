"""Real-model tool-use eval: how reliably does each local model USE tools?

    .venv\\Scripts\\python.exe -m tests.eval_tools [model-prefix ...] [--runs N]

Each case runs in a throw-away workspace with autonomy ON (so no pop-ups) and is scored on the real outcome
(files on disk, facts in the database), not on what the model says. Prints a pass-rate table per model and strategy.
"""
from __future__ import annotations

import sys
import time

from cerberus import learning  # noqa: F401  (registers the 'remember' tool)
from cerberus.agent_runtime import tool_loop
from cerberus.db import Database
from cerberus.ollama_client import OllamaClient, OllamaError, detect_ollama
from cerberus.safety import SafetyGate, safe_path
from cerberus.tools import ToolContext, tools_for_skills

from .helpers import Isolated

SKILLS = ["File Reading", "File Editing", "Memory"]


def _write(ctx, path, text):
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


CASES = [
    {"name": "write_file",
     "goal": "Create a file named hello.txt in the workspace containing exactly the text: Hail Cerberus",
     "check": lambda ctx, calls, text: safe_path("hello.txt").exists() and "Hail Cerberus" in safe_path("hello.txt").read_text()},
    {"name": "read_file",
     "setup": lambda ctx: _write(ctx, "secret.txt", "The vault code is 7391."),
     "goal": "Read the file secret.txt and tell me the vault code.",
     "check": lambda ctx, calls, text: "7391" in text and any(c == "read_file" for c in calls)},
    {"name": "remember",
     "goal": "Remember this about me: my favourite colour is crimson.",
     "check": lambda ctx, calls, text: any("crimson" in (r["value"] or "").lower() for r in ctx.db.query("SELECT value FROM facts"))},
    {"name": "list_files",
     "setup": lambda ctx: (_write(ctx, "alpha.md", "a"), _write(ctx, "beta.md", "b")),
     "goal": "List the files in the workspace and tell me their names.",
     "check": lambda ctx, calls, text: "alpha" in text and "beta" in text and "list_files" in calls},
    {"name": "no_tool",
     "goal": "What is the capital of France? Answer in one word.",
     "check": lambda ctx, calls, text: "paris" in text.lower() and not calls},
    {"name": "two_steps",
     "setup": lambda ctx: _write(ctx, "in.txt", "forty-two"),
     "goal": "Read in.txt and write its contents, uppercased, into out.txt.",
     "check": lambda ctx, calls, text: safe_path("out.txt").exists() and "FORTY-TWO" in safe_path("out.txt").read_text().upper()},
]


def run_case(client, model, case, strategy) -> tuple[bool, list[str], str, str, float]:
    with Isolated() as root:
        db = Database(root / "eval.db")
        gate = SafetyGate()
        gate.autonomous = True
        ctx = ToolContext(gate=gate, db=db, origin="local", actor="eval")
        if case.get("setup"):
            case["setup"](ctx)
        calls, text, used, t0 = [], "", "?", time.time()
        try:
            for ev in tool_loop(client, model, "You are a careful assistant with tools.", case["goal"],
                                tools_for_skills(SKILLS), ctx, max_steps=6, strategy=strategy):
                if ev["type"] == "tool_call":
                    calls.append(ev["name"])
                elif ev["type"] == "loop_final":
                    text, used = ev["text"], ev.get("strategy", "?")
            ok = bool(case["check"](ctx, calls, text))
        except OllamaError as exc:
            ok, text, used = False, f"ERROR {exc}", "error"
        finally:
            elapsed = time.time() - t0
            db.close()
        return ok, calls, text, used, elapsed


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 1
    args = [a for a in args if not a.isdigit()]
    url = detect_ollama()
    if not url:
        sys.exit("Ollama is not running.")
    client = OllamaClient(url)
    wanted = args or ["llama3", "mistral", "deepseek-r1"]
    table = []
    for w in wanted:
        model = client.resolve(w)
        if not model:
            print(f"-- {w}: not installed, skipped")
            continue
        for strategy in ("auto",):
            passed = total = 0
            print(f"\n=== {model}  (strategy={strategy}) ===")
            for case in CASES:
                for _ in range(runs):
                    ok, calls, text, used, secs = run_case(client, model, case, strategy)
                    passed += ok
                    total += 1
                    print(f"  {'PASS' if ok else 'FAIL'} {case['name']:<11} via {used:<7} {secs:5.1f}s calls={calls} -> {text[:70]!r}")
            table.append((model, passed, total))
    print("\nRESULTS")
    for model, p, t in table:
        print(f"  {model:<24} {p}/{t}")


if __name__ == "__main__":
    main()
