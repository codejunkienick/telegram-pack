# Contributing

Thanks for helping improve this pack. One copy of the code serves every
city that adopts it, each configured only by its own settings file, so
changes must stay true for every adopter.

## Run the tests

```sh
sh scripts/test.sh
```

The suite is offline and fake-only: it fakes Telegram, GC and whisper-cli,
uses temporary directories, and reaches no network. It needs no real
settings file and starts no unit. It needs Python 3.11 or later, and curl for the tests that run the real curl against a loopback server (they skip without it). Extra arguments go to unittest, for
example `sh scripts/test.sh -v`.

## Settings compatibility

Every settings key a live city's file already holds keeps loading with
the same meaning. A new behaviour is a new key with a default that
preserves the old behaviour; never repurpose or silently drop a key.
A settings problem refuses to load with one line naming it.

## What belongs here

- The pack names no city, person or company outside the marked example
  in the README, and holds no credential: tokens and secrets live in
  each city's own owner-only files, never in this repository.
- The pack ships no `[[command]]` handler. A command or button a city
  wants is that city's own handler program, registered in its own
  settings; see "City commands" in `bridge/OPERATIONS.md`.
- Every send goes through the delivery ledger; there is no direct
  send path around it.
- Behaviour changes carry a test in `bridge/` in the same change,
  using the existing fakes.

## Commit messages

Use `<type>(<scope>): <imperative summary>` with a short body that says
what changed and why, for example:

```text
fix(commands): Preserve the callback answer on handler failure

Keep the fixed failure notice and the empty callback answer in the
ledger order the dispatch already used.
```

Keep the subject at most 72 characters, with no final period. Add no
tool, model or AI attribution lines, and no internal tracker or task
ids. When the change fixes a GitHub issue of this repository, end the
body with `Refs #<n>`.
