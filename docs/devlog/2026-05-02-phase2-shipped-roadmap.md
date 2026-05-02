# Roadmap — what's left after Phase 2 chunk 1 ships (2026-05-02)

> Captured at the user's request after Phase 2 chunk 1 (label preview + apply) shipped as commit `c38061a`. Excludes `td-f037ae` (Phase 4: bulk unsubscribe) — that's parked until everything below is done.

## Code work

### 1. Phase 2 chunk 2 — undo

Architecture is already in place; `audit_log` captures pre-mutation state for every applied row.

- `undo` subcommand: `gmailwiz undo --run-id <id>` walks the run's audit_log rows where `status='applied'`, calls `modify_labels` with add/remove swapped, flips per-row status to `reverted` and run status to `undone`.
- Menu option 4 with the same prior-run picker pattern as option 3, but listing runs where `status='committed'` or `'partially_failed'` and `phase='label'`.
- Tests:
  - undo of a committed run
  - undo of a partially-failed run
  - refusing to undo a `planned` or already-`undone` run
  - drift safety (undo walks audit_log, not Gmail)
- **Estimated: 1–2 hours.**

### 2. Phase 3 — archive

Phase 2's design generalizes; mostly mechanical.

- `categories.gmail_archive_action` → remove `INBOX` label (no per-category mapping needed; archive is one action).
- `gmail_client`: archive uses existing `modify_labels(remove_label_ids=["INBOX"])` — no new method needed.
- `planning.build_archive_plan(--from-run-id <label-run-id>)` — strict scope rule per the implementation plan: archive candidates are messages from a specific *prior labeling run*, not "every message currently carrying a `gmailwiz/...` label." Persists `phase='archive'` runs row + audit_log rows.
- `apply_archive_plan` mirrors `apply_label_plan` (will probably DRY up shared loop machinery into `planning._apply_plan`).
- `archive` subcommand + `undo` extension to reverse archives (re-add `INBOX`).
- Menu options 6/7/8 for preview / apply / undo archive.
- Tests:
  - candidate scoping by `--from-run-id`
  - no archive without a prior label run
  - undo restores `INBOX`
- **Estimated: 4–6 hours including review cycle.**

## Validation

### 3. Real-world dry run of Phase 2 against the actual inbox before any `--commit`

Workflow:
1. `gmailwiz` → option 1 → scan ~200 unread to populate the senders cache.
2. Eyeball the categories. Reclassify any obviously wrong senders manually if needed (no built-in tool yet — direct SQL on `data/db/state.db`).
3. Option 2 → preview a `promotional` run → review the table.
4. Option 3 → apply. Spot-check Gmail UI to confirm labels landed correctly.
5. If anything looks off, the audit_log has the message_ids needed to manually un-label.

### 4. Decide on undo before or after real-world validation

If Phase 2 chunk 1 works clean against the real inbox first, undo can land later without urgency. If nervous, ship undo first.

## Open scope question

### 5. `td-650d79` — fun project vs. SaneBox replacement

This affects whether to invest in:

- Smarter scheduling (vs. interactive-only — currently parked due to 7-day Testing-mode token expiry).
- Rules / whitelists (e.g., "always promotional except sender X").
- A reclassify command (when Claude gets a sender wrong, currently the user edits SQLite by hand).
- Better display-name normalization (the `display_name` field is sometimes empty when senders rotate names).

User said on 2026-04-28: **"lean fun project, easy to extend later."** Worth re-deciding once Phases 2/3 ship and the tool has been in use for a few weeks.

## Operational / maintenance

### 6. 7-day OAuth Testing-mode token expiry

Token regenerates automatically as long as the user re-auths via menu option 5 within the window. No code work needed; awareness item.

### 7. Phase 2 devlog

Not written yet (this roadmap doc is not a session devlog). User's call whether to `/nn` after undo lands or sooner.

### 8. `td approve td-f10d21`

Close the Phase 2 task that's currently sitting in `in_review` because td blocks self-approval. Single command.

---

## Recommended ordering

Real-world dry run of Phase 2 (#3) → if clean, ship undo (#1) → ship archive (#2).

The scope question (#5) is best revisited after a week or two of actual use.
