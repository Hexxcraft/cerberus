# Cerberus user guide

The full tour of what Cerberus does and how to use it. For the short version, see the [README](../README.md); for the design, [ARCHITECTURE.md](ARCHITECTURE.md).

## Using it

Pick a rite on the left, type, press **Enter**. That is all. Everything else lives on the **Gates** tab and you never have to touch it.

| Rite | What it does |
|---|---|
| Forge | Builds software: Architect, Builder and Inspector produce a complete project; files are syntax-checked and auto-repaired. |
| Scribe | Editing, or ghost-writing in *your* voice (learned from samples you paste into Soul Ledger). |
| Seance | Everyday conversation: Id, Super-Ego and Ego deliberate and the Ego answers. |
| Tutor | Small steps, hands-on, calm tone, built for ADHD/autistic learners. |
| Treasury | Live quotes, watchlist, budget, plain-spoken analysis (not financial advice). |
| Hunt | Job search, resume and cover-letter tailoring, form-filling help. |

## Thinking: how the heads check each other

The **Thinking** selector (under the chat box, also on Gates and on the phone page) trades speed for accuracy. Speed is not the goal; the default is **Careful**.

| Level | What happens |
|---|---|
| Quick | Small talk is answered by one head with no checking. Everything bigger still uses all three. |
| Careful (default) | All three heads work. Then the two heads that did **not** write the answer fact-check it. If both flag a problem, each claim is still ruled on by the head that did not raise it; only claims that survive are confirmed, so false alarms are overruled. A confirmed problem sends the writer back to fix exactly that, for up to 2 rounds. Doubts that survive are shown to you under the answer instead of being hidden. |
| Meticulous | Like Careful with up to 3 rounds, and each step is also checked before the next head builds on it. Slowest and most reliable. |

Lessons from caught mistakes are remembered (up to 60), but only when the correction passed a second look, and are shown to the heads on later requests so they stop repeating them. The Gates tab shows the record (answers checked, mistakes caught and fixed, false alarms overruled) and a *Forget what it learned* button.
When memory is low, Cerberus quietly drops to Quick rather than crash.

Honest limits: three small local models checking each other catch many mistakes, not all. They can share the same blind spot, and a reviewer can be wrong. For facts that matter, use Cloud Handoff or check the source.

## Full computer access

Cerberus can list, read, search, write, append, move, delete and create folders anywhere on this computer (`pc_*` tools), and register your own programs to run. The rules:

- **Every change needs a unanimous vote**: Editor, Voice and Reasoner must each approve. The first refusal stops it. If a head cannot be reached or answers unclearly, the change is refused.
- **Then you are asked.** The pact pop-up shows what will change. No answer in 3 minutes means no.
- **Undoable.** "Undo that" in chat (or `pc_undo`) reverses a recent change. Deleted files go to Cerberus's undo folder, not oblivion. Overwritten files are saved first.
- **Always off limits**, even with access on, and refused before the heads or you are even asked:
  - Secrets can never be read or changed: SSH/cloud key folders, browser profiles, Windows credential stores, `.env`, `.pem`/`.key`/password-store files, and Cerberus's own data folder.
  - Nothing can be written, moved or deleted inside Windows, Program Files, ProgramData, AppData (Temp excepted) or Cerberus's program folder, or directly in a drive's top folder.
  - Your main folders (Desktop, Documents, Downloads...) can be written into but never moved or deleted as a whole, and neither can any folder containing a protected one.
  - Programs and scripts (`.exe`, `.bat`, `.ps1`, `.lnk` and similar) cannot be created or changed outside the workspace.
  - Network paths (`\\server\...`) and hidden-stream paths are refused.
- **On by default, but harmless without consent**: every change still needs the three heads and you. Phones and other devices can never use these tools, even with the token.
- Turn the whole thing off with the *Full computer access* switch on Gates. *Seal the Gates* (top right) stops everything pending instantly.
- **Autonomous Agent Mode** (header) skips the prompts to you. It is never saved and is OFF every launch. The heads' vote still happens.

Reading is capped at about 200,000 characters per file and secret files are never read. The undo folder keeps the last 200 changes.

## Made for your hardware

At start Cerberus reads your hardware (CPU, RAM, GPU/VRAM). On a strong machine (for example 32 GB RAM and a 16 GB graphics card) it uses a 16k-token memory window per answer and models kept loaded for 30 minutes. The **Gates > This computer** panel shows what it found, free memory right now, and a *Tune for this computer* button. Weaker machines get smaller windows automatically. It never starts a big task when free memory is low.

## The Warden (idle-time watchdog)

Watches the window in front of you. While a game (by program name or folder) or one of your listed websites (by browser window title) is up and you are actually at the keyboard (input within 5 minutes), it counts the time as wasted, adds it to a tally, and nags:

- First alert after 10 minutes, then at shrinking gaps (7, 5, 4, 3, 2, 2, 1 minutes). Each pop-up is blunt, shames you a little, and reminds you of your three goals: **body** (work out), **mind** (study), **soul** (make something of your own). The wording comes from your profile (see PROFILE.md).
- You can explain yourself. The generalist and the editor must **both** accept the reason; if so you get a pass of 5-60 minutes (max 3 excuses per game/site per day). If Ollama is down a simple keyword check stands in. Two rejected excuses jump straight to the final warning.
- No answer within the reply time, or alert 5 onwards, gives a **LAST WARNING** with a countdown. If it runs out, the game is closed politely (a close request first, forced after 8 seconds). For a website only the **tab** is closed (Ctrl+W); the browser is never terminated.
- The pop-up's close button refuses. You can answer, click *Fine. Close the game.*, or close the thing yourself.
- Programs on the "never nag" list are exempt while they are in front (the list is editable; one game is pre-listed as an example).
- The **Warden** tab shows today / week / all-time totals, the biggest sinks, a recent log, and all the lists and numbers, which you can edit. Edits are accepted only from this PC (`local_only`); the AI heads, their tools, and your phone cannot change them or close anything through the Warden. It is off for the test suite and can be turned off with the switch.

Never closed: browsers, system programs, anything in the Windows folder, Cerberus itself.

### Hardware guard

`cerberus/guard.py` watches heat from up to three sources and acts on the worst one: the **graphics card** (read straight from the NVIDIA driver's NVML library; `nvidia-smi` is only a fallback), the **motherboard's thermal zones** plus Windows' own "slowing down to stay cool" flag (performance counters, no admin needed), and the real **CPU temperature** if you run [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) with *Options > Remote Web Server* on (port 8085; Cerberus only ever talks to `127.0.0.1`). Warm (graphics card 78 C, processor 85 C) slows Cerberus down; hot (86 C / 95 C) pauses heavy work until it cools. Low memory or a nearly full drive refuse big jobs, and every program Cerberus runs for you is limited (memory ceiling, process cap, time limit, low priority). Status: Warden tab, or `GET /api/guard`. The limits can be tuned in settings under `guard` but are clamped to safe ranges and cannot be switched off.

## Cloud Handoff (offline when you want, big model when you need it)

Add a key in **Gates** (OpenAI, Anthropic, Gemini, xAI, DeepSeek, Mistral or OpenRouter) and flip **Cloud Handoff** in the header. The three local heads still think; Cerberus then compresses their output into one token-budgeted prompt and sends only that to the cloud model. If the cloud call fails, you get the local answer. With the switch off, nothing ever leaves the machine.

## Your profile

Who Cerberus works for, what it calls you, and your daily rhythm come from a profile file that lives outside the project. See [PROFILE.md](PROFILE.md).

## Tabs

- **Communion** - chat. You see only the answer; the stages, thinking, council notes and technical errors are hidden unless you tick *Show the workings* (off by default; the phone page has the same box). Tick *Let Cerberus act* to let the rite use tools. *Attach files...* shows files you picked on the Library tab as a chip above the message box. The Thinking selector is here.
- **Library** - browse this PC (starting places, up one folder, preview) and attach text or Word (.docx) files to your next message; every head reads them for that one request. Secrets, Cerberus's own private data and network paths are never listed or read; with *Full computer access* off only the workspace is reachable. At most 8 files, about 12k characters each; PDFs are not supported. Local use only: a phone cannot browse or attach.
- **Legion** - agents with a name, role, task and schedule (`daily at 9am`, `every 30 minutes`) that run in the background.
- **Soul Ledger** - what it has learned about you (facts, habits, writing style, bond level). Edit or erase anything.
- **Treasury** - watchlist and budget.
- **Grimoire** - GitHub token, git status/commit/push/pull/clone, create repos, open in VS Code.
- **Warden** - the wasted-time tally, what it watches, how strict it is, and the hardware guard's readout.
- **Chronicle** - the mistakes Cerberus remembers (with a *Forget all of it* button) and the review report built from its run log, with *Copy report* and *Open log folder*. Logging can be switched off here.
- **Gates** - Thinking, this computer, the council's record, full-access switch, plugins, your projects, Resume Studio, safety log, models and per-rite model upgrades, cloud keys, LAN info, workspace folder, backups.

## Knowing itself, learning from errors, and the review log

- **It knows what it can do.** Every head is told, in plain facts built from the live tool list, whether actions are on for this message, what a phone chat cannot do, and whether Cloud Handoff is on; it is told never to claim it did something without a tool result. Ask "what can you do?" and it answers from the real list (`GET /api/capabilities`, `?text=true` for prose).
- **It learns from errors.** Tool calls with bad arguments, unknown tools, refused changes, off-limits targets and repeated tool failures are written down (secrets scrubbed, de-duplicated, capped at 80). The lessons that would change its behaviour are shown to the tool-using Hand before it acts again. Environment trouble (Ollama down, too hot, a crash) is kept for the review only. See or erase it on the Chronicle tab (`GET`/`DELETE /api/mistakes`).
- **It logs everything for review.** One line per request goes to `%LOCALAPPDATA%\Cerberus\logs\runs-YYYYMM.jsonl`: rite, thinking level, models, time per step and per tool, time to the first word, tool errors, what the heads caught, doubts left, cloud use, outcome (ok / error / cancelled / abandoned), and for crashes the redacted technical trace that the chat itself hides. Requests and answers are kept only as ~200-character previews (switch off with `telemetry.previews`), API keys and tokens are scrubbed, files older than 6 months are deleted, and nothing leaves the PC. Read it with `python -m cerberus.telemetry [--days N] [--json] [--run ID]`, the Chronicle tab, or `GET /api/telemetry/report` (local only). The report ends with "what to tweak".

## Resume Studio (Hunt)

Your Resume project is wired into Hunt as a plugin-style skill; its code is never imported, each step runs in a separate limited process. Set the project folder, a base resume (.docx or text) and your contact details on **Gates > Resume Studio**. Hunt then has four tools, all local-only:

- `resume_status` - is everything ready, and what is missing.
- `resume_find_jobs` - searches your own feeds if set, else the built-in Remotive/Arbeitnow boards, and keeps a **numbered** list.
- `resume_tailor <number>` - rewrites your own resume bullets for that job and saves a Word file in the Resume project's `output` folder (a plain template is made in Cerberus's data folder if the project has none). Names and numbers that are not in your base resume are flagged as possible inventions; this is a heuristic, not a guarantee.
- `resume_stage <number>` - opens that job's application page in a visible browser and fills only the contact fields you saved. It never uploads a file and never submits; you review and press submit. It needs all three heads and your approval, refuses private addresses, and the job can only come from the stored list, never from a model-supplied URL.

## Rituals and notifications

Scheduled daily rituals (Morning Forge, Mind & Soul Pass, Reading Hour, Blackout warning and Blackout) post Windows notifications and show on the phone page. Toggle them on Gates.

## Plugins and your own projects

- **Plugins**: drop a `.py` file in `%LOCALAPPDATA%\Cerberus\plugins` (the Gates tab has an *Open plugins folder* button; `_template.py` there shows the format) and restart. A plugin registers tools with the same `@tool` decorator the built-ins use, so they get the same safety prompts and the heads' vote when marked `council=True`. Only install plugins you wrote or trust: they run with your permissions. The models cannot write plugins (that folder is protected).
- **Projects**: on Gates, *Your projects*, register a name, folder and run command (for example `python main.py`). The heads can then run it by name with the usual vote and your approval; they cannot invent a command. Nothing project-specific is built in.

## Use it from your phone

Run **`scripts\start_lan_server.bat`**. It prints the address (e.g. `http://192.168.1.20:8777`) and your API token, and can add a Private-network firewall rule. Open the address in the phone browser, enter the token once, and chat; you can install it as an app from the browser menu. The phone page has the rite and Thinking selectors and shows the heads checking each other. Network devices can chat and approve or seal, but cannot change settings, manage agents, turn on autonomy or use any computer, project or desktop tool. Browser notifications on the phone need `https` or `localhost` and arrive only while the page is open (no background push).

Also available: `python main.py --lan`.

## Data, backups and safety

- Data lives in `%LOCALAPPDATA%\Cerberus` (override with the `CERBERUS_DATA_DIR` environment variable). Settings secrets (API keys, GitHub token) are encrypted with Windows DPAPI and are never in backups.
- Gates > Backups creates and restores a zip of your history and settings. A backup is made automatically once a day (last 7 kept). Restores take effect on the next start.
- File tools in a normal rite are limited to the **workspace** folder (default `workspace\`). Web tools refuse local/private addresses.
- Every tool call is written to `audit.log` in the data folder.
- Desktop control (mouse and keyboard) is experimental; keep confirmations on.
- If you ran an earlier version, your old data may still sit in `data.migrated` inside the project folder as plain text. It was copied, not deleted; once you are happy, delete that folder yourself.

## Known limits

- Local models are slow to load; Careful and Meticulous are deliberately slower. DeepSeek-R1 as a checker can take minutes.
- In a small test set, Llama 3 passed 10/12 prompts, Mistral 8/12 and DeepSeek-R1 3/12 (it often runs out of thinking budget), which is why the council uses all three for different jobs rather than trusting any one.
- Reading screens (`screen_read`) uses the `moondream` vision model; the mouse/keyboard path is covered by mocked tests only.
- Warden: sites are detected from the browser window title only (no real URL), and closing a tab (Ctrl+W) is mock-tested only, not against a live browser. Games get a polite close then a forced one, so unsaved progress can be lost; anti-cheat games may refuse. Exclusive-fullscreen games can hide the pop-up (use borderless; a message box appears after 15 s). A gamepad does not count as presence. All processes sharing the game's exe path are closed.
- Hardware guard: Windows does not expose the CPU's own temperature to normal programs. Without LibreHardwareMonitor running, the processor is covered only indirectly (motherboard sensors on the development PC read about 28 C and 17 C at idle, so they are board/case sensors, not the chip, plus Windows' throttling flag). The LibreHardwareMonitor reader is tested against a sample of its data format, not against the real program. Cerberus has no overclock or power tools, the chips have their own protection, and anything run outside Cerberus is not covered.
- CrewAI is not used; Cerberus has its own small orchestrator.
- Small local models cannot catch a fact none of them know (for example, a wrong Nobel laureate or year); they can agree on the same wrong answer. Unresolved doubts are shown under the answer on purpose. For facts that matter use Cloud Handoff or check a source.
- Attached files: text and Word (.docx) only (no PDF). Resume Studio is tested with mocks only; the staging browser has not been exercised against a live job site. The Library, Chronicle and Gates tabs were checked against a live server but their visual layout was not screenshot-verified.
- Resume Studio needs `python-docx` (in `requirements.txt`) to make Word files and `playwright` to stage forms.
