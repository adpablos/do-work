# PRD drafts

Drop intent-level PRDs here to feed into `do work`. This folder is draft-only — the real source of truth lives under `do-work/user-requests/` once the skill captures it.

## Convention

1. Write or paste a PRD as `docs/prd/<short-name>.md`. Focus on intent: what and why, not how. No pre-numbered REQs, no file paths, no implementation plans.
2. Run `do work @docs/prd/<short-name>.md` in your assistant. The skill captures the PRD into a User Request (UR) folder plus one or more REQ files under `do-work/`.
3. Delete the draft. The UR is now canonical.

## Why drafts aren't committed

`docs/prd/*.md` is gitignored. The PRD is a throwaway handoff format — once the skill has captured it, the UR folder preserves the verbatim input forever. Keeping the draft around creates two sources of truth for the same intent, and they drift.

`README.md` in this folder is the exception — it's tracked so the convention stays visible.
