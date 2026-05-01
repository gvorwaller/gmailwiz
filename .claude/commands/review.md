---
name: adversarial-review
description: Hostile code review loop - find bugs, fix them all, re-review until clean, then ask to commit
tags: [review, quality, debugging]
---

# Adversarial Code Review & Remediation Loop

You are now in **hostile code review mode**. Your job is to find bugs, fix every one, and keep reviewing until the code is clean.

## Step 0: Determine Review Scope

**Argument provided:** `$ARGUMENTS`

Interpret the argument as follows:
- `branch` or `full` → Review ALL changes on the current branch vs the base branch (main). Use `git diff main...HEAD` plus any uncommitted changes.
- `uncommitted` or `local` → Review only uncommitted/staged changes. Use `git diff` and `git diff --cached`.
- If no argument or unrecognized → **Ask the user**: "Should I review the full branch diff (all commits since main) or just uncommitted changes?"

Once scope is determined, gather the list of changed files and their diffs. Store this as your review target.

---

## Step 1: Adversarial Review (Delegated to Sub-Agent)

Use the **Task tool** to spawn a sub-agent (`subagent_type: "general-purpose"`) with the following instructions:

> You are a **hostile adversarial code reviewer**. Your only job is to find bugs — not to be helpful, not to be polite.
>
> Review the following changed files: [list the files and their diffs]
>
> Execute EVERY check in this checklist:
>
> ### Return Value Audit
> For EVERY function call in the changed code:
> - Read the actual function source (not just the signature)
> - Verify the return type matches how it's being used
> - Check if wrapper functions modify the return shape
>
> ### Type Boundary Check
> For ANY data crossing boundaries (DB, API, JSON):
> - PostgreSQL JSONB returns as objects, not strings
> - Verify JSON.parse() has typeof guards
> - Check for implicit type coercion
> - PostgreSQL NUMERIC returns as strings — verify arithmetic operations use parseFloat/Number
>
> ### Null/Undefined Propagation
> - What happens if this returns null/undefined?
> - Trace the value through ALL downstream consumers
> - Does optional chaining (?.) mask real errors?
>
> ### Call Site Analysis
> - Use grep to find ALL callers of modified functions
> - Do existing callers handle new return types/errors?
> - Are there tests that now have wrong assumptions?
>
> ### Async/Await Verification
> - Is every async function call properly awaited?
> - Are there Promises being used as values without await?
> - Check for missing await on database queries
>
> ### Assumption Inventory
> List every assumption made in this code:
> - "This function returns X" — Did you verify by reading the source?
> - "This field exists" — Is it guaranteed?
> - "This won't be null" — What makes you certain?
>
> ### Security & Edge Cases
> - SQL injection, XSS, command injection risks?
> - Missing input validation at system boundaries?
> - Error messages leaking internal details?
>
> ### UI Convention Compliance (if frontend files changed)
> - NO toast notifications (react-toastify, sonner, etc.) — use styled confirmation modals
> - Follows reddit-intelligence CSS class patterns
> - Modals follow the standard modal-overlay/modal-content pattern
>
> **Output Format — You MUST use this exact format:**
>
> ```
> ## P1 CRITICAL (Must fix before commit)
> [Each issue: file:line, description, and what the fix should be]
>
> ## P2 IMPORTANT (Should fix)
> [Each issue: file:line, description, and what the fix should be]
>
> ## P3 NITPICK (Consider fixing)
> [Each issue: file:line, description, and what the fix should be]
>
> ## VERIFIED CORRECT
> [Things you checked and confirmed are actually right]
>
> TOTAL: X P1, Y P2, Z P3
> ```
>
> Be ruthless. Assume every line has a bug until proven otherwise. If you didn't read the source, you don't know the return type.

**After the sub-agent returns**, display the full review findings to the user.

---

## Step 2: Fix All Issues

Work through the findings systematically:

1. **P1 CRITICAL** — Fix ALL of these. No exceptions.
2. **P2 IMPORTANT** — Fix ALL of these.
3. **P3 NITPICK** — Fix ALL of these.

For each fix:
- Read the relevant source code before editing
- Make the minimal correct fix (no over-engineering)
- Briefly note what you changed and why

After all fixes are applied, summarize what was fixed:
```
Round N fixes applied:
- P1: [count] fixed — [brief descriptions]
- P2: [count] fixed — [brief descriptions]
- P3: [count] fixed — [brief descriptions]
```

---

## Step 3: Re-Review (Loop)

After fixing all issues, spawn **another sub-agent review** (repeat Step 1) targeting the same files plus any new files touched during fixes.

**Critical:** The re-review sub-agent gets fresh context — it does NOT know what the previous review found. This prevents confirmation bias.

Evaluate the re-review results:
- **If P1 or P2 issues remain** → Go back to Step 2. Fix everything. Then re-review again (Step 3).
- **If only P3 issues remain** → Fix them, then do ONE final review pass.
- **If the review comes back clean (0 P1, 0 P2, 0 P3)** → Proceed to Step 4.

**Maximum iterations:** 5 rounds. If still not clean after 5 rounds, proceed to **Step 3.5** before presenting to the user.

Display a running scoreboard:
```
Review Round 1: 3 P1, 5 P2, 2 P3 → Fixed all
Review Round 2: 0 P1, 1 P2, 1 P3 → Fixed all
Review Round 3: 0 P1, 0 P2, 0 P3 → CLEAN
```

---

## Step 3.5: Final Sanity Validation

**When to run:** After either (a) the loop hits the 5-round cap with remaining issues, or (b) the loop exits clean. This catches both false positives from the hostile reviewer AND any issues the adversarial cycle may have introduced through its own fixes.

Spawn a **different kind of sub-agent** (`subagent_type: "general-purpose"`) — this one is NOT hostile. It is an **objective validator**:

> You are an **objective code validator**. You are NOT an adversarial reviewer — your job is to verify claims, not manufacture findings.
>
> **Context:** An adversarial review loop has completed N rounds of review-and-fix on these files. Below are the remaining flagged issues (if any) and a summary of all fixes applied during the review cycle.
>
> [Include: remaining issues from the final round, list of all files modified, summary of fixes applied]
>
> **Your tasks:**
>
> ### 1. Validate Remaining Issues
> For each remaining P1/P2/P3 issue flagged by the hostile reviewer:
> - Read the actual source code at the cited location
> - Determine if the issue is **REAL** (actual bug, genuine risk, or legitimate concern) or a **FALSE POSITIVE** (code is correct, reviewer was being overzealous, or the concern is theoretical with no practical impact)
> - For each issue, state your verdict with a brief explanation
>
> ### 2. Verify Fix Integrity
> Quickly scan ALL fixes applied during the review cycle to check that no fix introduced a new problem:
> - Did any fix break an adjacent code path?
> - Did any fix change behavior beyond the minimal correction?
> - Are all fixes consistent with each other (no contradictions)?
>
> **Output Format:**
> ```
> ## Remaining Issue Validation
> - [issue description] → REAL / FALSE POSITIVE — [1-sentence reason]
> - [issue description] → REAL / FALSE POSITIVE — [1-sentence reason]
>
> ## Fix Integrity Check
> [PASS: All fixes look correct / PROBLEM: description of any fix-introduced issue]
>
> ## Final Verdict
> REAL issues remaining: N
> False positives dismissed: N
> Fix integrity: PASS/FAIL
> ```

**After the validator returns:**
- If **fix integrity fails** → fix the identified problem, then re-run this validation step once
- If **all remaining issues are false positives** → mark the review as CLEAN and proceed to Step 4
- If **real issues remain** → fix them (no re-review needed; the validator already confirmed they're real), then proceed to Step 4
- Display the validation results to the user as part of the scoreboard:

```
Review Round 5: 0 P1, 2 P2, 1 P3 → Fixed all
Sanity Validation: 2 false positives dismissed, 1 real fix applied, fix integrity PASS
Final Status: CLEAN
```

---

## Step 4: Codex Cross-Review (Independent Second Opinion)

**When to run:** After Step 3.5 completes and all Claude-side review rounds are done.

This step sends the reviewed files to OpenAI Codex (GPT-5.4) for an **independent cross-review** — a completely separate AI with no knowledge of the prior review rounds. This catches blind spots that Claude's adversarial loop may share due to common training biases.

**Invoke the `codex:codex-rescue` agent** with a task prompt structured as follows:

> Review the following files for bugs, type safety issues, async/await correctness, null propagation, and security concerns. These files were just modified during an adversarial review cycle — your job is to find anything the prior reviewers missed.
>
> **Files to review:** [list all files modified during the review cycle, plus any files they directly consume or are consumed by]
>
> **Repository context:** Svelte frontend application (no database). Focus on reactivity correctness, store subscriptions, lifecycle issues, and Svelte-specific patterns (`$:` reactive declarations, `onMount`/`onDestroy` cleanup, component binding edge cases). Read the project's CLAUDE.md for additional context.
>
> **Output format — use this exact structure:**
> ```
> ## P1 CRITICAL (Must fix before commit)
> [Each issue: file:line, description, evidence from source code]
>
> ## P2 IMPORTANT (Should fix)
> [Each issue: file:line, description, evidence from source code]
>
> ## P3 NITPICK (Consider fixing)
> [Each issue: file:line, description, evidence from source code]
>
> ## VERIFIED CORRECT
> [Things you checked and confirmed are actually right — include file:line references]
>
> TOTAL: X P1, Y P2, Z P3
> ```
>
> Be grounded: every finding must cite the specific file and line number, and explain what the actual bug or risk is with evidence from the source. Do not speculate without reading the code.

**After the Codex agent returns**, analyze the findings:

1. **For each P1/P2 finding**: Read the cited source code yourself. Determine if the issue is REAL or a FALSE POSITIVE.
2. **Fix all confirmed REAL issues** — minimal correct fixes only.
3. **Dismiss false positives** with a brief explanation.
4. **Do NOT re-run the full Claude adversarial loop** — Codex findings are already grounded with citations. Just fix and verify.

Display the Codex cross-review results as part of the scoreboard:

```
Review Round N: 0 P1, 0 P2, 0 P3 → CLEAN
Sanity Validation: X false positives dismissed, Y real fixes applied, fix integrity PASS
Codex Cross-Review: A P1, B P2, C P3 found → D real, E false positives → Fixed D
Final Status: CLEAN
```

**If Codex is unavailable** (not installed, not authenticated, or the run fails): skip this step and note it in the final summary. Do not block the review on Codex availability.

---

## Step 5: Pre-Commit Summary & User Approval

**Do NOT commit automatically.** Present the user with:

```
## Review Complete — Ready for Commit

### Scope: [branch/uncommitted]
### Rounds: N review-fix cycles to reach clean (+ Codex cross-review)

### Files Modified:
[list all files that were changed during remediation]

### Summary of All Fixes Applied:
[grouped by category: return values, type safety, null handling, async/await, etc.]
[note which fixes came from Codex cross-review vs Claude adversarial rounds]

### Final Review Status: CLEAN (0 P1, 0 P2, 0 P3)

Shall I commit these changes? If yes, what commit message would you like?
(Or I can suggest one based on the changes.)
```

Wait for explicit user approval before committing. If the user approves:
- Stage only the files that were part of the review/fix cycle
- Use the user's preferred commit message
- Do NOT push unless explicitly asked

---

## Mindset Throughout

- Assume every line has a bug until proven otherwise
- "Works on my machine" is not verification
- If you didn't read the source, you don't know the return type
- Optimism is the enemy of correctness
- Never declare something fixed without verifying end-to-end
- The re-review agent is trying to break YOUR fixes — that's the point
