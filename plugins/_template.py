"""Cerberus plugin template. Copy this file, rename it (no leading underscore), edit, and restart Cerberus.

A plugin adds one or more tools the heads can use. Keep each tool small and honest about its risk:
  risk="safe"     only looks at things
  risk="write"    changes files or runs something (you are asked to confirm)
  risk="publish"  sends something out of this computer
  risk="control"  moves the mouse or runs a program on your screen
  local_only=True blocks the tool when the request comes from your phone.
  council=True    all three heads must approve each use (do this for anything that changes things).
Never put secrets in this file.
"""
from cerberus.tools import ToolContext, tool


@tool("hello_plugin", "My Plugin", "Say hello. Replace this with what your tool really does.",
      {"name": "who to greet"}, risk="safe")
def hello_plugin(ctx: ToolContext, name: str = "world") -> str:
    return f"Hello, {name}! (from a plugin)"
