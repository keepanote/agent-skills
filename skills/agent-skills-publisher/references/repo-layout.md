# Repo Layout

## Goal

Keep one canonical copy of every skill in Git, then install local links for each AI tool instead of maintaining duplicate copies.

## Recommended layout

```text
repo-root/
  skills/
    skill-a/
      SKILL.md
      agents/openai.yaml
      scripts/
      references/
    skill-b/
      SKILL.md
  scripts/
    install-skill-links.ps1
    install-skill-links.sh
  .github/workflows/
```

## Why this layout

- `skills/` is the only Git-tracked source of truth.
- Install scripts rebuild local links on each machine.
- Codex- and Claude-specific install directories stay outside normal Git content.
- Adding a second or third skill does not require a repo redesign.

## Install targets

- Codex user-level:
  `~/.codex/skills/<skill-name>`
- Codex repo-style local skills:
  `~/.agents/skills/<skill-name>`
- Claude Code user-level:
  `~/.claude/skills/<skill-name>`

## Rules

- Do not commit Windows junctions or machine-local symlinks.
- Do not scatter canonical skills across `.codex/`, `.claude/`, or home directories.
- Keep each skill self-contained under its own folder.
- Keep installer scripts at repo root level under `scripts/`.
- Prefer plain-text docs and stdlib-only helper scripts when possible.
