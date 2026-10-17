# Contributing

Thanks for looking. This started as a personal tool, so the bar is "keep it safe and keep it tested".

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .     # offline, no Ollama needed
.\.venv\Scripts\python.exe -m ruff check .
```

The tests use a fake Ollama (`tests/helpers.py`) and an isolated data folder, so they never touch your real data. Tests set
`CERBERUS_NO_WARDEN=1` and `CERBERUS_NO_TELEMETRY=1`.

## Rules that must keep holding

1. **Consent.** Tools that change anything are risk-rated; PC-wide changes are `council=True` (three-head vote) and go through the
   SafetyGate. Unclear answers mean "no".
2. **Local only.** Anything that reads the PC, logs or settings secrets is `local_only` and must be refused for remote clients.
3. **No secrets in output.** Keys and tokens never reach logs, API responses or backups.
4. **Quiet UI.** Technical detail stays out of the normal chat; it goes to the run log.
5. **Stable keys.** Rite keys (`coding`, `writing`, `conversation`, `teaching`, `wealth`, `jobs`) are stored identifiers. Rename labels only.
6. **No personal data in the repo.** The operator profile lives in the data folder (see [docs/PROFILE.md](docs/PROFILE.md)); only the fictional example profile is committed.

## Pull requests

Small and focused, with tests. Describe the behaviour change, not just the diff. Be kind in reviews.
