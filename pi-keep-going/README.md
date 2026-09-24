# keep-going — Pi extension: say "continue" when the agent stops

Not a Claude Code hook: this one is for the [Pi coding agent](https://github.com/earendil-works/pi)
(CLI and any front-end that loads `~/.pi/agent/extensions`). When a run stops mid-task, it appends a
`continue` message and asks Pi for one more model request — no typing, no clicking Submit.

## Install

```bash
cp keep-going.ts ~/.pi/agent/extensions/keep-going.ts
```

New Pi sessions pick it up automatically; in a running one type `/reload`.
Try it without installing: `pi --extension ./keep-going.ts`.

## When it continues — and when it does not

It runs on Pi's `agent_before_settle` event (the last point before the agent goes idle).

| Situation | Result |
|---|---|
| Agent stopped (finished a turn or hit an error) | sends `continue`, one more run |
| You aborted the run (Esc) | does nothing — you meant it |
| The agent's last message ends with `?` | does nothing — it is waiting for your answer |
| Already auto-continued `max` times in a row (default 5) | stops and shows a warning; any real message from you resets the count |
| Print / JSON mode (`pi -p`, scripts) | does nothing — "continue" would race the prompt |
| Hook is off | does nothing |

The cap is the runaway-cost guard: an unconditional "continue" loop can burn tokens forever.

## Control

State lives in `~/.pi/agent/keep-going.json` and is shared by every session (default: on, max 5).

```
/keepgoing              show status
/keepgoing on | off     enable / disable
/keepgoing max 10       consecutive auto-continues allowed (1-50)
/keepgoing text keep going, finish the task     change the message sent
```

`KEEP_GOING_CONFIG=/path/file.json` overrides the config location (used by the tests).

## Test

```bash
node test-keep-going.mjs      # 27 checks, temp config, Node 22.18+ (strips the TS types itself)
```

Also verified once against a live Pi in RPC mode: the agent said "part one done.", the extension
injected a `keep-going` message, a second run started, and the cap then stopped it.

## Note

If you also use a startup extension that stops Pi Desktop (e.g. an `auto-continue.ts`), run test
sessions with `pi -ne -e ./keep-going.ts` so that extension is not loaded and your Desktop stays up.
