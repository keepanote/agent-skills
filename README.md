# agent-skills

Shared repository for reusable AI agent skills.

This repository keeps each skill in one canonical location under `skills/`.
Installers then link those skills into the directories used by Codex and Claude Code on each machine.

## Repository layout

- `skills/`
  Canonical skill folders tracked in Git.
- `scripts/install-skill-links.ps1`
  Windows installer that creates junctions for Codex and Claude Code.
- `scripts/install-skill-links.sh`
  Ubuntu/macOS installer that creates symlinks for Codex and Claude Code.
- `.github/workflows/ci.yml`
  Basic cross-platform validation.

## Current skills

- `codex-log-disk-guard`

## Example prompts

- `帮我检查 ~/.codex/logs_2.sqlite 是否因 TRACE 日志持续高频写盘；如果中招，先备份，再用 SQLite trigger 拦截 logs 表 insert，并 checkpoint/truncate WAL，最后采样确认 MAX(id) 和 WAL 不再增长`
- `Inspect ~/.codex/logs_2.sqlite for a TRACE write storm, back it up, block new logs inserts with a SQLite trigger, checkpoint and truncate WAL, then sample MAX(id) and WAL until both stay flat.`

## Install on Windows

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-skill-links.ps1
```

## Install on Ubuntu or macOS

```bash
chmod +x ./scripts/install-skill-links.sh
./scripts/install-skill-links.sh
```

## Publish to GitHub

```powershell
cd D:\code\agent-skills
git init
git add .
git commit -m "Add shared AI skills repository"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

## Compatibility notes

- Codex:
  Install into `~/.codex/skills/` for user-level discovery.
- Claude Code:
  Install into `~/.claude/skills/` for user-level discovery.
- Repository-local discovery:
  Codex uses `.agents/skills/` in a repo.
  Claude Code uses `.claude/skills/` in a repo.
  This repository keeps canonical content in `skills/` and uses installer scripts to create local links, which is more reliable than committing Windows junctions.
