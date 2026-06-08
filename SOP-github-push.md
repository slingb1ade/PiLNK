# PiLNK — Standard Operating Procedure
## Pushing Updates to GitHub & the Fleet

**Owner:** AJ (sole authority — Rule #27)
**Last updated:** 2026-06-08
**Reference release (worked example):** v1.2.11.1 "Vitals"

---

## 1. Purpose

To ship PiLNK changes to GitHub and out to the node fleet **the same correct, methodical way every time** — so every release is tested before it ships, the version/OTA machinery stays in sync, and nothing reaches a tester's node half-finished or broken.

This SOP is the canonical checklist. If a step is skipped, stop and go back to it.

---

## 2. Roles — who does what

| Role | Who | Responsibilities | Hard limits |
|---|---|---|---|
| **Authority** | AJ | Decides what ships. Picks the **version number + codename**. Performs **all git commits/pushes** (authenticates to GitHub). Approves every service restart. | — |
| **Builder** | Claude Code | Writes/edits the feature on the dev box. First-pass testing. | Builds only — the release gates below still apply. |
| **Release supervisor** | Hub Claude (via PiLNK Hub MCP) | Runs the read-only verification gate, syncs `version.php`, confirms the live endpoint, verifies OTA, drafts tester comms. | **Cannot push** (read-only git). **Only restarts services when AJ explicitly asks** (Rule #29). |

> The push is always AJ's hands. Neither Claude can push to GitHub — by design.

---

## 3. Boxes — label every command with its box

| Box | Role |
|---|---|
| **Pi4** | Dev / test bench. Where new features are built and first tested. |
| **Pi5 (EpsomPi)** | Production node **and the single source of truth for GitHub pushes**. Final testing happens here; the canonical commit is pushed from here. |
| **myHost** | pilnk.io PHP backend. Home of `api/version.php`. No shell access. |
| **linklabs** | AJ's laptop / workstation. |

- **GitHub:** `github.com/slingb1ade/PiLNK` — branch **`main`**
- **Rule:** only ever push from **Pi5**. Don't push from Pi4. Develop on Pi4, bring the change onto Pi5, then push from Pi5 as the single canonical commit.

---

## 4. Core principles (carried over from how we work)

1. **Backup-first** — know your revert before you change anything.
2. **One change at a time, test each before the next** — never stack an untested change on an untested change.
3. **Surgical edits** — match exactly, change only what's needed, verify each edit.
4. **Ask, then STOP** — at any decision point, confirm before proceeding.
5. **Label every command with its box** (Pi4 / Pi5 / myHost / linklabs).
6. **Global by default** (Rule #25) — every feature gets a global behaviour check before it's called done.

---

## 5. The pipeline at a glance

```
Build (Pi4, Claude Code)
   -> Dev test (Pi4)
   -> Install + final test (Pi5)
   -> [GATE] Global behaviour check (Rule #25)
   -> Version bump: VERSION file (Pi5)            (Rule #28, AJ picks number/codename)
   -> Commit & push to GitHub main (Pi5, AJ)
   -> [GATE] Verify push landed (Rule #31, Hub Claude)
   -> Sync version.php $RELEASE_META (myHost, Hub Claude)
   -> Confirm live /api/version.php endpoint
   -> OTA: fleet auto-updates within ~5 min
   -> Tester comms if needed (Rule #30)
```

---

## 6. Step-by-step procedure

### Phase 0 — Before touching anything
- State plainly **what's changing and why**. Prefer **one feature/fix per release**.
- List the **affected files** and **which box(es)** they live on.
- Confirm your **revert path** (git, file backup, or `.bak`).

### Phase 1 — Build & dev test (Pi4)
- Build/edit the feature on **Pi4** (Claude Code or by hand).
- Test on **Pi4**: feature works as intended, **no regressions**, no console/JS errors, service healthy.
- **Do not proceed** until Pi4 is solid. This is "one thing at a time."

### Phase 2 — Install & final test (Pi5)
- Bring the Pi4-tested change onto **Pi5** and confirm it matches what passed on Pi4.
- Restart the service **only if AJ asks** (Rule #29). *(Jinja templates are cached — a template change needs a `pilnk` restart to load; a browser refresh alone won't do it.)*
- Hard-refresh (Ctrl+Shift+R) and **verify on the production node**: feature works + no regressions.
- **[GATE] Global behaviour check (Rule #25)** — state how it behaves for:
  - **AJ-Auckland (NZ)**, **Jim-SD (US)**, **KICTPI-Wichita (US)**, **M0CRT-UK (UK)**
- **Do not proceed** until Pi5 is confirmed.

### Phase 3 — Version bump (Rule #28)
- **AJ picks** the new **version number** and **codename**.
- Bump the **`VERSION`** file in the Pi5 repo to the new number (bare number, e.g. `1.2.11.1`).
- **Do not** hand-edit a version *number* in `version.php` — it **auto-reads** the number live from the GitHub raw `VERSION` file (5-min cache). Only `$RELEASE_META` is edited, and that comes later (Phase 6).

> Rule #28: a Pi-side ship and the version metadata must move together, or the OTA updater silently does nothing.

### Phase 4 — Commit & push (Pi5, AJ — manual)
On **Pi5**, in `~/pilnk`:
```bash
git add <changed files> VERSION
git commit -m "vX.Y.Z: <concise description>"
git push origin main
```
AJ authenticates. This is the **single canonical commit**.

### Phase 5 — [GATE] Verify the push landed (Rule #31 — Hub Claude, read-only)
Before `version.php` is touched, confirm the code is actually on GitHub `main`:
```bash
# Pi5 (read-only)
git status                          # -> clean, "up to date with 'origin/main'"
git log -1 --oneline                # local HEAD
git log origin/main -1 --oneline    # must match local HEAD
```
- If there are **uncommitted or unpushed** changes → **STOP. Push first.**
- **Do not edit `version.php` until this gate passes.**

> Rule #31 exists to prevent version-sync mismatch loops: version.php must never advertise a version whose code isn't on GitHub yet.

### Phase 6 — Sync `version.php` `$RELEASE_META` (myHost — Hub Claude)
Edit only `$RELEASE_META` in `api/version.php`:
- `codename` — the chosen codename
- `released` — today's date (YYYY-MM-DD)
- `required` — `true` **only** for a forced update; otherwise `false`
- `notes` — plain, user-facing changelog (this surfaces in the in-app update notice)

Then verify:
```bash
# myHost
php -l api/version.php              # (via php_lint) -> no syntax errors
```

### Phase 7 — Confirm the live endpoint
```bash
# fetch https://pilnk.io/api/version.php
```
Confirm `version` (auto-read), `codename`, `released`, and `notes` are all correct.

### Phase 8 — OTA verification
- Nodes poll `/api/version.php` every **~5 min**; if remote version > local, the node `git pull`s and updates itself.
- Confirm the fleet picks it up within a poll cycle (fleet status / per-node version).

### Phase 9 — Tester comms (if relevant)
- Forum copy is **plain text** (Rule #30 — Quill flattens formatting; ALL-CAPS headers, plain dashes, no markdown `**`).
- **Multi-line commands don't survive Quill** — it collapses newlines into one line. Instead: **host a script** on pilnk.io and give testers a **single-line `curl … && bash …`**. One line can't be flattened.
- Global: consider every affected operator, not just the one who reported it.

---

## 7. Quick-reference card (the TL;DR)

```
[ ] Pi4: build + dev test — solid, no regressions
[ ] Pi5: install + final test + hard-refresh
[ ] Pi5: GLOBAL CHECK (AJ-NZ / Jim-US / KICTPI-US / M0CRT-UK)   (#25)
[ ] Pi5: bump VERSION file (AJ picks number + codename)         (#28)
[ ] Pi5: git add … VERSION ; git commit ; git push  (AJ pushes)
[ ] Pi5: VERIFY pushed — git status / log / origin match        (#31)  <-- GATE
[ ] myHost: edit version.php $RELEASE_META + php_lint
[ ] confirm https://pilnk.io/api/version.php is correct
[ ] OTA: fleet updates within ~5 min
[ ] testers: plain text; single-line curl for commands          (#30)
```

---

## 8. Rollback

- **Code:** `git revert <commit>` (then push from Pi5), or per-node `git reset` to the prior commit; restore any `.bak` files.
- **VERSION:** revert the `VERSION` file and push, or the OTA will keep advertising the new number.
- **version.php:** restore the prior `$RELEASE_META` (date + notes).
- Backups made in Phase 0 are the safety net — keep them until the release is confirmed healthy across the fleet.

---

## 9. Rules referenced

| Rule | Summary |
|---|---|
| **#25** | Global by default — every feature gets a global behaviour check (AJ-Auckland, Jim-SD, KICTPI-Wichita, M0CRT-UK) before it's done. |
| **#27** | AJ has unilateral authority; may override/amend any rule with a single statement. |
| **#28** | Pi-side ship requires `VERSION` + `version.php` metadata in sync. (version.php auto-reads the *number* from GitHub raw VERSION; only `$RELEASE_META` is hand-edited. AJ picks the number.) |
| **#29** | Services are restarted **only** when AJ explicitly asks in that turn. |
| **#30** | Forum/announcement copy defaults to plain text (Quill). Single-line curl for commands. |
| **#31** | Before editing `version.php`, verify Pi5 has pushed the code to GitHub `main`. If not, STOP and push first. |

---

*This SOP reflects the process used to ship v1.2.11.1 "Vitals" (aircraft-labels + node-marker render fixes) on 2026-06-08.*
