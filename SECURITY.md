# Security policy

Cerberus can read and change files and control the mouse and keyboard of the computer it runs on, so security reports are taken
seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting: *Security > Report a vulnerability* on this repository.
Include what you found, how to reproduce it, and the impact. You will get an answer as soon as I can manage; this is a one-person
project.

## What counts

Examples of real problems: a way around the path denylists, a remote (phone/LAN) client reaching a `local_only` endpoint or a PC
tool, secrets appearing in logs/API responses/backups, a way to run a change without the council vote or user approval, SSRF through
the web tools, or command injection through a tool argument.

## Design in brief

- Everything above `safe` risk needs the user's approval; PC changes also need a unanimous vote of all three local models. Uncertainty
  is treated as refusal.
- The LAN server uses a bearer token and treats non-loopback clients as remote: no PC tools, file browsing, log access, or settings
  that hold secrets.
- Secrets are sealed with Windows DPAPI and excluded from backups. Logs scrub keys and tokens.
- Autonomous mode (skips only the user prompt) is never persisted and is off at every launch.
- See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the layers and [tests/](tests/) for the behaviours that are pinned down.

## Known limits

Local models can be wrong or manipulated by hostile text (prompt injection) in a web page or file they read. That is why file
changes, shell commands, git pushes and desktop control are gated by consent regardless of what a model says. Plugins run with your
permissions: install only code you trust. Run it on a machine and network you control.
