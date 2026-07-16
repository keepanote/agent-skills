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
- `agent-skills-publisher`
- `sysclean`

## 中文说明

这个仓库用于集中管理多个 AI skill。真实 skill 正本统一放在 `skills/` 目录下，再通过安装脚本链接到 Codex 和 Claude Code 的技能目录，避免维护多份副本。适合把常用排障、发布、研究、自动化类 skill 都收敛到一个 GitHub 仓库里。

## Example skill prompts

- `Use $codex-log-disk-guard to inspect ~/.codex/logs_2.sqlite, block runaway TRACE inserts, checkpoint WAL, and verify that MAX(id) stops growing.`
- `使用 $codex-log-disk-guard 检查 ~/.codex/logs_2.sqlite，必要时备份、阻断 logs 写入、truncate WAL，并确认 MAX(id) 不再增长。`
- `Use $agent-skills-publisher to install this shared skill repo for both Codex and Claude Code, then prepare it for GitHub publishing.`
- `使用 $sysclean 压缩进程工作集并清理临时文件以释放内存;默认不清空回收站,需要时加 -IncludeRecycleBin。`

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
