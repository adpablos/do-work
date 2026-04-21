# Do-Work Skill Project

## Versioning and Changelog

The version on `main` is the version users actually get via `npx skills add`. It reflects **releases**, not intermediate commits. Version bumps and changelog entries happen at release time, not per commit on a feature branch.

### Semver

- Patch (0.1.0 → 0.1.1): Bug fixes, minor tweaks
- Minor (0.1.0 → 0.2.0): New features, behavior changes
- Major (0.1.0 → 1.0.0): Breaking changes

When in doubt, bump the patch version.

### On `main` directly (hotfixes, docs, small standalone changes)

One commit = one release. Before committing:

1. Bump the version in `actions/version.md` (line starting with `**Current version**:`).
2. Add a corresponding entry at the top of `CHANGELOG.md` (format below).
3. Commit.

### On feature / `prd/` branches (the usual workflow for anything non-trivial)

Follow the [Keep a Changelog](https://keepachangelog.com) `[Unreleased]` pattern:

1. **Do not touch `actions/version.md`** during normal commits on the branch.
2. **Do not add versioned entries to `CHANGELOG.md`** during normal commits. Instead, accumulate bullets under a `## [Unreleased]` section at the top of the file. Each commit that introduces user-visible change adds to that section.
3. Review-feedback commits on an open PR don't need new bullets unless they change behavior.
4. When the PR is ready to merge: a **single final commit on the branch** promotes `## [Unreleased]` to `## X.Y.Z — [Name] (YYYY-MM-DD)`, bumps `actions/version.md` once, and becomes the release commit.

This keeps the version from inflating while a branch is under review, avoids merge conflicts between parallel branches on the version line, and ensures each number on `main` corresponds to something users actually received.

### Changelog entry format

```markdown
## X.Y.Z — The [Fun Two-Word Name] (YYYY-MM-DD)

[1-2 sentences. Casual, clear, and concise. What changed and why it matters. No fluff, but personality is welcome.]

- [Bullet points for specifics — what was added, changed, or fixed]
```

Rules:

- **Newest on top.** The file reads top-to-bottom as newest-to-oldest.
- **Give every release a name.** A short, fun title after the em dash (e.g., "The Organizer", "Typo Patrol"). Two or three words max.
- **Date every entry.** `(YYYY-MM-DD)`. Use the merge date.
- **Lead with the value, not the implementation.** "The archive tidies itself now" beats "added cleanup.md".
- **Keep it brief.** One short paragraph + a few bullets. If you're writing more than 5 bullets, you're over-explaining — unless it's a big release (e.g., a whole UR consolidated), in which case group bullets by theme.
- **Match the voice.** Conversational, not corporate. Imagine you're telling a friend what shipped.
- **Every release gets an entry.** No skipping.

### Why this replaces the old "bump per commit" rule

The old rule made sense when every commit hit `main` directly. Once work moved to feature branches with PR review, bumping per commit created inflated version numbers that never corresponded to a release (e.g., the parallel-safety branch accumulated eleven bumps 0.14.0 → 0.22.0, none of which users ever saw). The `[Unreleased]` pattern keeps branch work honest and makes the version on `main` mean something.

## Agent Compatibility

This skill is designed to work with **any agentic coding tool**, not just one specific platform. When writing or editing action files:

- **Use generalized language.** Say "use your environment's ask-user prompt/tool" rather than naming a specific tool API. Say "spawn a subagent" rather than referencing a specific tool's agent mechanism.
- **Hint at advanced features, don't require them.** Subagents, multi-agent workflows, and structured question UIs improve results when available. The actions must still work in a single-session tool that reads the markdown as a prompt.
- **Each action file should work as a standalone prompt.** If someone pastes `do.md` into a basic chat interface with file access, the instructions should be clear enough to follow without the SKILL.md routing layer or any skill-runner infrastructure.
- **No tool-specific APIs in action files.** Tool-specific names and APIs belong in the tool's own integration layer, not in the skill's action files. Use platform-specific details only as clearly-labeled examples (e.g., "Example: Claude Code caches images at `~/.claude/...`").
- **Design for the floor, not the ceiling.** The least sophisticated agent that can read/write files and run shell commands should be able to execute these actions correctly. Advanced agents benefit from subagents and parallel execution, but the baseline must work without them.
