"""Importing this package registers every tool in the registry."""
from . import browser, computer, desktop, files, finance, git_tools, jobs, projects, resume, shell, vision, web  # noqa: F401  (imports register the tools)
from .base import REGISTRY, Tool, ToolContext, run_tool, tool, skills_catalog, tool_prompt, tools_for_skills  # noqa: F401
