# Validation Checklist

## Before publishing

1. Confirm every skill lives under `skills/<skill-name>/`.
2. Confirm every skill has a `SKILL.md`.
3. Confirm Codex-facing skills include `agents/openai.yaml` when appropriate.
4. Confirm installer scripts run without destroying unrelated local folders.
5. Confirm user-level install targets are created for Codex and Claude Code.
6. Confirm CI checks basic script syntax and `--help` entrypoints.
7. Confirm platform-specific scripts are gated by OS in CI.

## For multi-skill repositories

1. Add a second skill and rerun installers.
2. Verify both skills appear under:
   `~/.codex/skills/`
   `~/.agents/skills/`
   `~/.claude/skills/`
3. Verify the second skill does not require changes to repo layout.
4. Verify Git status remains clean after install; local links should not be tracked.

## Publish gate

The repository is ready to publish when:

- canonical skill content is only under `skills/`
- local install links exist and resolve correctly
- CI covers every skill that contains scripts
- README explains installation and compatibility clearly
