# Making Cerberus yours: the profile

Cerberus ships with a neutral example profile (`cerberus/seed/profile.example.json`, a fictional "Alex Example"). A fresh clone works with it out of the box, and no personal data is ever part of the repository.

To use your own details, create a `profile.json` in the Cerberus data folder (`%LOCALAPPDATA%\Cerberus`, or whatever `CERBERUS_DATA_DIR` points to) and restart. Copy the example as a starting point.

## Where Cerberus looks

1. The file named by the `CERBERUS_PROFILE` environment variable
2. `profile.json` in the data folder
3. The bundled example

If your file is missing, is not valid JSON, or is not a JSON object, Cerberus quietly falls back to the next choice, so a typo never stops it from starting.

## Fields

| Field | Meaning |
|---|---|
| `version` | Whole number. Raise it to load changed facts into an existing install (see below). |
| `name` | Your full name. Used for the user name if you have not set one. |
| `plain_name` | What Cerberus calls you when it is being blunt. Defaults to your first name. |
| `honorifics` | Titles it may use for you in normal replies (`["Boss", "Chief"]`). |
| `facts` | `[key, value, category]` triples loaded into the Soul Ledger. |
| `dossier` or `dossier_file` | Free text about you, or a file next to the profile that holds it. |
| `voice` | Instructions for how Cerberus talks to you (tone, length, how hard to push). |
| `goals` | Exactly three `[label, text]` pairs the Warden reminds you of. |
| `goal_words` | Words that count as touching one of your goals (optional). |
| `core_fact_keys` | Facts that are always included in the model's prompt; the rest are picked by relevance. |

## How loading works

On first launch, and whenever the profile `version` is higher than the one already loaded, Cerberus:

- stores the dossier text,
- adds each fact to the Soul Ledger **without overwriting a fact you edited** (facts are keyed by name),
- sets your user name if it is blank.

Edit or delete facts any time on the Soul Ledger tab. "Forget what it learned" erases what Cerberus picked up from chatting but keeps your profile facts.

## Privacy

Everything in the profile stays on your machine. It is sent to a cloud model only if you turn on Cloud Handoff, and then only as part of the compressed prompt. Never put a profile with personal details inside the project folder; the data folder is outside the repository and is git-ignored by design.
