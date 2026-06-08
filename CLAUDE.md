# CLAUDE.md — PiLNK

Persistent project context for Claude Code. Read at the start of every session.
**Full release procedure:** see `SOP-github-push.md` (committed alongside this file).

---

## What PiLNK is

ADS-B (and planned VHF-ATC) flight tracking that runs on distributed nodes — Raspberry Pi and small x86 boxes — under the LinkLabs umbrella (link-labs.io). Public site: **pilnk.io**. Single developer: **AJ**.

**Stack:** Flask node dashboard (`:5000`) · decoders `dump1090-fa` / `readsb` / `airspy_adsb` · OTA auto-updater · MySQL + PHP backend on myHost · Resend (email).

---

## Who does what

- **AJ** — sole authority (Rule #27). Picks every **version number + codename**. Performs **all git pushes** (authenticates to GitHub). Approves every service restart.
- **You (Claude Code)** — the **builder**. Write and test features on the dev box. Follow the gates below; never ship past them on your own.
- **Hub Claude** (claude.ai + PiLNK Hub MCP) — **release supervisor**: runs the read-only verify gate, syncs `version.php`, confirms the live endpoint, drafts tester comms.

---

## Boxes — label every command with its box

- **Pi4** — dev / test bench. Build + first test here.
- **Pi5 (EpsomPi)** — production node **and the only box you push from**. Final test here.
- **myHost** — pilnk.io PHP backend (`api/version.php`). No shell access.
- **linklabs** — AJ's laptop.
- **GitHub:** `github.com/slingb1ade/PiLNK`, branch `main`. **Only ever push from Pi5.**

---

## How we work (non-negotiable)

1. **Backup-first** — know the revert before changing anything.
2. **One change at a time**; test each before the next.
3. **Surgical edits** — change only what's needed; verify each.
4. **Ask, then STOP** — confirm at decision points; don't barrel ahead.
5. **Label every command** with its box (Pi4 / Pi5 / myHost / linklabs).
6. **Global by default** (Rule #25).

---

## The Rules

- **#25 — Global by default.** Every feature gets a global behaviour check before it's "done": state how it behaves for **AJ-Auckland (NZ)**, **Jim-SD (US)**, **KICTPI-Wichita (US)**, **M0CRT-UK (UK)**.
- **#27 — AJ authority.** AJ may override or amend any rule with a single statement. Proceed when he does.
- **#28 — Version sync.** A Pi-side ship requires the **`VERSION`** file bumped. `version.php` **auto-reads** the version *number* from the GitHub raw `VERSION` — do **not** hand-edit a number there. Only its `$RELEASE_META` (codename/released/required/notes) is edited, by Hub Claude, **after** the push. AJ picks the number + codename.
- **#29 — Restart on request only.** Restart a service (e.g. `pilnk`) **only** when AJ explicitly asks in that turn. Never as part of a build.
- **#30 — Plain-text tester comms.** Forum/announcement copy is plain text (the Quill editor flattens formatting and collapses multi-line). For commands testers must run, host a script and give a **single-line** `curl … && bash …`.
- **#31 — Git-first.** Before `version.php` is touched, the code must be confirmed pushed to GitHub `main` (`git status` clean + local HEAD == `origin/main`). If anything is unpushed, **STOP and push first.**

---

## Release flow (condensed — full detail in `SOP-github-push.md`)

```
[ ] Pi4: build + dev test — solid, no regressions
[ ] Pi5: install + final test + hard-refresh
[ ] Pi5: GLOBAL CHECK (AJ-NZ / Jim-US / KICTPI-US / M0CRT-UK)   (#25)
[ ] Pi5: bump VERSION file — AJ picks number + codename          (#28)
[ ] Pi5: git add … VERSION ; commit ; push   (AJ pushes)
[ ] VERIFY pushed — git status / log / origin match             (#31)  <-- GATE
[ ] myHost: version.php $RELEASE_META synced + php -l clean  (Hub Claude / AJ)
[ ] confirm https://pilnk.io/api/version.php
[ ] OTA: fleet updates within ~5 min
```

**Do not push without AJ. Do not bump the version without AJ's chosen number + codename.**

---

## Gotchas (things that have bitten us)

- **Jinja templates are cached** — a change to `templates/*.html` needs a `pilnk` restart to load; a browser refresh alone won't show it. (Restart = AJ's call, #29.)
- **Canvas plane renderer** — aircraft draw on a single `<canvas>` (`SHOW_DOM_PLANES = false`), so there are **no per-plane DOM markers**. Any map feature (labels, receiver marker, click handling) must **not** depend on `markers[cs]` existing. This caused the v1.2.11.1 labels + node-marker bugs.
- **myHost has no shell** — use MySQL queries / PHP endpoints, never shell tailing. There is no per-directory `error_log` file; PHP errors land in the **`error_logs` DB table**.
- **Resend = 2 requests/sec** — space notification email sends so a multi-recipient fan-out doesn't 429.
- **Pi4 → Pi5 hand-off** — develop on Pi4, bring the change onto Pi5, then push from Pi5 as the single canonical commit. Don't push from Pi4.

---

## Working style

Slow, methodical, surgical — one verified step at a time. Match that pace; don't batch unverified changes. 🦙🐐
