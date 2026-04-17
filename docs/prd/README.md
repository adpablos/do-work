# PRD drafts

Drop intent-level PRDs here to feed into `do work`. This folder is draft-only — the real source of truth lives under `do-work/user-requests/` once the skill captures it.

## Convention

1. Write or paste a PRD as `docs/prd/<short-name>.md`. Focus on intent: what and why, not how. No pre-numbered REQs, no file paths, no implementation plans.
2. Run `do work @docs/prd/<short-name>.md` in your assistant. The skill captures the PRD into a User Request (UR) folder plus one or more REQ files under `do-work/`.
3. Delete the draft. The UR is now canonical.

## Why drafts aren't committed

`docs/prd/*.md` is gitignored. The PRD is a throwaway handoff format — once the skill has captured it, the UR folder preserves the verbatim input forever. Keeping the draft around creates two sources of truth for the same intent, and they drift.

`README.md` in this folder is the exception — it's tracked so the convention stays visible.

## Meta-use: when the PRD targets the skill itself

When a PRD describes changes to `do-work` itself (the code under `SKILL.md` and `actions/`), the capture-and-run flow is recursive: the skill modifies its own source. A few guardrails:

- **Work on a branch.** `git checkout -b prd/<short-name>` before capture. If an execution breaks the skill, you revert the branch; you don't lose trunk.
- **Current session uses the old skill.** When a session starts, `SKILL.md` and the action files load as prompt context. Edits made by `work run` don't take effect until the **next** session. Don't chain REQs that depend on each other's skill edits inside one session.
- **One REQ per session when the skill is the target.** Run a REQ, end the session, smoke-test in a fresh session (e.g. `do work add test`), then continue. Batching hides breakage here.
- **Resolve open questions before `run`.** The work action plans each REQ independently. Cross-cutting architectural decisions need to be settled first — otherwise REQs make incompatible assumptions.
- **Verify captured REQs against the live skill.** `verify-request` compares REQs vs the PRD, not vs the current code. Skim the REQs after capture to confirm they still make sense given what's actually in `actions/` today.
