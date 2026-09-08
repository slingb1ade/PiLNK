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
| **Authority** | AJ | Decides **what ships and when**. Holds the push kill-switch (`PILNK_MCP_PI5_GIT_PUSH_ENABLED` in `/opt/pilnk-mcp/.env`). Approves every service restart. | — |
| **Builder** | Claude Code | Writes/edits the feature on the dev box. First-pass testing. | Builds only — the release gates below still apply. |
| **Release supervisor** | Hub Claude (via PiLNK Hub MCP) | Owns the release mechanics end to end: version bump, commit, **push**, the Rule #31 verify, `version.php` sync, live-endpoint confirmation, OTA check, changelog, tester comms. | **Only restarts services when AJ explicitly asks** (Rule #29). Push is double-gated — see below. |

> **Changed 8 September 2026.** Push used to be AJ's hands only. It now sits with
> the release supervisor, because splitting one release across three actors was
> where changes went missing — work sat uncommitted in a tree that `update.sh`
> hard-resets, and the site changelog silently fell four releases behind.
>
> **The push is double-gated and both gates are AJ's:**
>
> 1. `PILNK_MCP_PI5_GIT_PUSH_ENABLED=true` must be set in `/opt/pilnk-mcp/.env` —
>    outside every tool's jail, so Claude cannot read, set or clear it. Removing
>    that line revokes push instantly, no code change.
> 2. `push=True` must be passed explicitly on the call. It defaults to `False`,
>    so a push can never happen as a side effect of a commit.
>
> The tool runs `git push origin <branch>` and nothing else — current branch,
> no flags, shell-quoted. Force-push, branch deletion and `--mirror` are not
> reachable through it. This is why push was **not** added to the
> `pi5_run_command` whitelist: that list is prefix-matched, so `git push` there
> would also have authorised `git push --force` and `git push origin :main`
> against the branch the whole fleet OTA-updates from.
>
> **AJ still decides what ships.** Claude states what it is about to push and why,
> before passing `push=True`. The flag removed the SSH round-trip, not the
> checkpoint.

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

### Phase 4 — Commit & push (Pi5 — Hub Claude, via `pi5_git_commit_push`)
One call does the whole thing. Stage **named paths only** — never everything:
```
pi5_git_commit_push(
  message = "vX.Y.Z Codename — <concise description>",
  paths   = ["VERSION", "<changed files>"],
  push    = True
)
```
This is the **single canonical commit**. Notes:

- **Dry-run first if the staging is at all unclear:** same call with
  `dry_run=True` returns `would_stage`, `status` and a `diffstat` and touches
  nothing. It short-circuits before the push block, so it confirms *staging*,
  not the push gate.
- **`VERSION` goes in the same commit as the code.** Rule #28 — they must move
  together.
- **Untracked files are never staged.** Anything new (a model, an asset) has to
  be named explicitly in `paths` or it stays behind and no other node gets it.
- **`git reset --hard origin/main` in `update.sh` destroys uncommitted work.**
  Anything left modified in the Pi5 tree dies at the next OTA. If it is worth
  keeping, commit it.
- If push is refused, `pushed` comes back `false` with `push_skipped` explaining
  why — normally the `.env` flag being off. The commit still succeeded and is
  safe locally; it just has not left the Pi.

Manual fallback, if the MCP is down — on **Pi5**, in `~/pilnk` (AJ):
```bash
git add <changed files> VERSION
git commit -m "vX.Y.Z: <concise description>"
git push origin main
```

### Phase 5 — [GATE] Verify the push landed (Rule #31 — Hub Claude)
Before `version.php` is touched, confirm the code is actually on GitHub `main`:
```bash
# Pi5
git status -sb                      # -> "## main...origin/main", no ahead/behind
git log origin/main -1 --oneline    # must be the commit just made
```
**Then verify against GitHub itself, not just the local refs:**
```
http_get_external("https://api.github.com/repos/slingb1ade/PiLNK/contents/VERSION?ref=main")
  -> content is base64; decode and check it is the version just shipped
```
- `git log origin/main` reads Pi5's **local copy** of the remote ref. It is
  accurate right after a push, but it is not independent evidence. The API call
  asks GitHub. Use both.
- **Do not use `raw.githubusercontent.com` for this gate.** It is CDN-cached and
  has been observed serving the old `VERSION` for 5–10 minutes after a confirmed
  push, including with a cache-busting query string. It is what `version.php`
  reads, so it matters — but as a *timing* consideration, not as verification.
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
[ ] Pi5: pi5_git_commit_push(paths=[VERSION, …], push=True)     (#32)
[ ] Pi5: VERIFY pushed — status -sb + GitHub API, not raw CDN   (#31)  <-- GATE
[ ] myHost: edit version.php $RELEASE_META + php_lint
[ ] confirm https://pilnk.io/api/version.php is correct
[ ] site: add the changelog.html entry                          (#33)
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
| **#31** | Before editing `version.php`, verify Pi5 has pushed the code to GitHub `main`. Check `git status -sb` **and** the GitHub API — not `raw.githubusercontent.com`, which is CDN-cached. If not pushed, STOP and push first. |
| **#32** | Push is the release supervisor's, double-gated: the `.env` flag (AJ's, outside every jail) **and** an explicit `push=True` per call. State what is being pushed and why before passing it. Never add `git push` to the `pi5_run_command` whitelist — prefix matching would authorise `--force` and branch deletion. |
| **#33** | A release is not finished until `changelog.html` on the site carries it. On 8 Sep 2026 the site changelog was found four releases behind (stopped at v1.4.1), so the entire international-NOTAM story had shipped to the fleet but never to readers. |

---

*Originally written around the process used to ship v1.2.11.1 "Vitals" on 2026-06-08.*

*Revised 2026-09-08 (v1.4.10 "Full-Circle"): push moved from AJ to the release
supervisor behind a double gate, Phase 4 rewritten around `pi5_git_commit_push`,
Rule #31 hardened to check the GitHub API rather than the CDN, and Rules #32/#33
added. The trigger was a session in which a fix sat uncommitted in a tree that
`update.sh` hard-resets, the site changelog had silently fallen four releases
behind, and two features (Airport View, Airfields) had been dead fleet-wide for
two months without anyone noticing.*
