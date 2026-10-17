## What and why

## Checklist
- [ ] `python -m ruff check .` is clean
- [ ] `python -m unittest discover -s tests -t .` passes
- [ ] New or changed tests cover the behaviour
- [ ] Any new tool that reads or changes the PC is `risk`-rated, goes through the SafetyGate (and the council vote if it changes things), and is `local_only` if a phone must not use it
- [ ] No secrets, personal data or machine-specific paths added
- [ ] README / CHANGELOG updated if behaviour changed
