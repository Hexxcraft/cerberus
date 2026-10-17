# Changelog

All notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [1.0.0] - 2026-10-17

First public release. Includes a full pre-release audit: security hardening, data-layer and GUI robustness fixes, a lint pass, and a privacy change.

### Changed (pre-release audit)
- **Privacy:** personal details moved out of the code into an operator profile file outside the repository. The repo ships a fictional example profile (`cerberus/seed/profile.example.json`); `CERBERUS_PROFILE` or `profile.json` in the data folder overrides it, and a broken file falls back to the example. See `docs/PROFILE.md`.
- **Security:** wider path denylists, a workspace guard for file tools, SSRF and injection fixes, remote-origin restrictions on settings and PC tools, audit-log coverage.
- **Reliability:** database and backup fixes, scheduler and Warden locking fixes, GUI polling guards (no overlapping polls, stale results discarded), safer window closing (process path re-checked before termination), startup retry for slow servers.
- **Docs:** README rewritten; the detailed manual moved to `docs/USAGE.md`; added screenshots.
- Tests grew to 314 offline tests; lint is clean.

### Added
- Three-head orchestration over local Ollama models (reasoner, generalist, editor) with six rites: Forge, Scribe, Seance, Tutor, Treasury, Hunt.
- Council: answers are fact-checked by the two heads that did not write them; false alarms are overruled; surviving doubts are shown.
- Consent-gated full computer access: unanimous three-head vote plus user approval, hard denylists, undo folder, audit log.
- Optional Cloud Handoff (OpenAI, Anthropic, Gemini, xAI, DeepSeek, Mistral, OpenRouter) with a token-budgeted prompt.
- Desktop GUI (CustomTkinter) and a phone page / PWA served over the LAN with a bearer token.
- Quiet chat by default: only answers are shown; a *Show the workings* switch reveals stages and council notes.
- Capability self-knowledge generated from the live tool registry (`GET /api/capabilities`).
- Error memory: scrubbed, de-duplicated tool-error lessons fed back to the tool-using Hand.
- Review log: one JSONL line per request plus `python -m cerberus.telemetry` report with tuning suggestions.
- Library tab: browse the PC and attach text / .docx files as prompt context (local clients only).
- Resume Studio for Hunt: find jobs, tailor a resume, stage an application form (never submits or uploads).
- Warden (idle-time watchdog) and hardware Guard (GPU/board/CPU heat, memory and disk limits, limited child processes).
- Plugin mechanism, project registry, daily rituals, encrypted secrets (DPAPI), backups.
- Offline test suite (fake Ollama), CI on GitHub Actions, standalone Windows build script.
