# The Laws of the Craft

> The first utterance of the Nerd Harness: a fixed boot banner, then a rotating *law of the*
> *craft* from the deck — codex-original aphorisms on debugging, humility, working code, and
> finishing. No borrowed quotes, no attributed sages (where a line is genuine folklore, like
> Murphy's Law, it is flagged as such and nothing more).

## The boot banner (fixed)

What [`bin/law`](bin/law) prints above the rotating law, unchanged every session:

```text
  >_  THE NERD HARNESS

      No gods, no gurus, no cargo cult.
      Working code decides.
```

## The rotating law

Beneath the boot line, the harness prints one rotating law from [`laws.txt`](laws.txt) — a
law of the craft, changing daily. Edit [`laws.txt`](laws.txt) (one law per line) to curate
or extend the deck. Keep them original, falsifiable, and short enough to read before coffee.
The current deck:

> - The machine keeps no secrets and grants no benefit of the doubt — it runs what you actually wrote, and waits for you to notice the difference.
> - The bug is never where you're certain it is; certainty is exactly where it hides.
> - Read the error message as if it were telling the truth, because it is.
> - If you didn't run it, you don't know it works — you only hope it does.
> - A green test that guards nothing is a lie you tell yourself on a schedule.
> - Clever is code you write on Monday and cannot read by Friday.
> - You cannot fix what you cannot summon on command.
> - The manual costs ten minutes; skipping it costs the afternoon.
> - Ninety percent done is the polite name for the easy half.
> - When the debugger lies to you, print the truth and read it slowly.
> - Every line you're proud of is a bug you haven't been introduced to yet.
> - Blame the compiler last — it has been wrong far less often than you have.
> - Rewrite it and you inherit every bug you forgot you already fixed.
> - "Done" is earned by running it, not announced by feeling it.

*The harness emits this first, on startup — [`bin/law`](bin/law).*
