# Hooks — keeping the disciplines present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and boots each run with the
law of the craft.

## `session-start.sh`

Emits, to stdout:

1. The **boot line** + a rotating **law of the craft** (`bin/law`, drawn from `laws.txt`).
2. The **conduct block** — the four disciplines, precedence, and the hard limit
   (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/nerd-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The boot line goes first, the disciplines stay present.

## Why there is no live hook here

The sibling editions run their gate a second time as a `PreToolUse` or `Stop` hook. This one
does not: [`gate/no_cargo_cult.py`](../gate/no_cargo_cult.py) mutates files in place and runs
the whole suite once per mutant, which no hook budget covers and no editing session should have
happening underneath it. The reasoning, and the smaller thing that would fit and is not done, is
in the README under *Why there is no live hook here*.

## Just want to see it?

```sh
./hooks/session-start.sh
```
