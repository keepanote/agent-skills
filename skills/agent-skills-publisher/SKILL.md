---
name: agent-skills-publisher
description: Publish and install shared AI skill repositories for Codex and Claude Code. Use when Codex needs to organize multiple skills under one repo, place canonical skill folders under `skills/`, create or refresh installer links into `~/.codex/skills`, `~/.agents/skills`, and `~/.claude/skills`, add GitHub publishing files, or validate that a multi-skill repository structure works on Windows, Ubuntu, and macOS.
---

# Agent Skills Publisher

Use this skill when the task is about packaging, linking, validating, or publishing a repository that stores multiple skills for Codex and Claude Code.

## Workflow

1. Keep canonical skill content only under `skills/`.
2. Avoid storing Windows junctions or platform-specific symlinks in Git history.
3. Add installer scripts that recreate local links for each machine.
4. Ensure each skill has its own `SKILL.md`.
5. Add `agents/openai.yaml` for skills that should present cleanly in Codex.
6. Validate basic entrypoints and installer behavior before publishing.

## Conventions

- Repository root:
  Keep shared automation under `scripts/` and CI under `.github/workflows/`.
- Skill root:
  Put each skill in `skills/<skill-name>/`.
- Codex install targets:
  `~/.codex/skills/` and optionally `~/.agents/skills/`.
- Claude Code install target:
  `~/.claude/skills/`.

## Example user prompts

- `Use $agent-skills-publisher to move my local skills into a shared Git repo and install them for Codex and Claude Code.`
- `Use $agent-skills-publisher to add a second skill, validate the repo structure, and prepare the repo for GitHub.`
