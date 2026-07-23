Spawn a **Haiku** agent (model: "haiku") to run the requested baseball script and relay its output. Use the Agent tool with `model: "haiku"`.

Map $ARGUMENTS (or infer from recent conversation context) to a command:

| keyword(s) | command |
|---|---|
| `preview` / `dry` / no arg | `python send_digest.py --dry-run --no-refresh` |
| `refresh preview` / `dry refresh` | `python send_digest.py --dry-run` |
| `send` / `email` | `python send_digest.py` |
| `fetch` / `refresh` | `python fetch_data.py` |
| `recap preview` / `recap dry` | `python weekly_recap.py --dry-run --no-refresh` |
| `recap` | `python weekly_recap.py` |
| `recalibrate` / `calibrate` | `python recalibrate_scores.py` |

Agent prompt template:
```
Run this command in C:\Users\katzs\Desktop\baseball and report the result:

  <resolved command>

Show the last 15 lines of stdout/stderr. If it errors, show the full traceback.
Working directory: C:\Users\katzs\Desktop\baseball
```

After the agent finishes, relay its output summary to the user in a few lines.
