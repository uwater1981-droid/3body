# 3body — Multi-Agent Orchestration Framework

A framework for running three AI agents (OpenClaw, Codex, Claude Code) as a coordinated pipeline to autonomously execute projects.

## Architecture

```
engine.py (runs every 10s via launchd)
  ├── scan()      — read all task files, check agent process liveness
  ├── decide()    — deterministic routing rules (no band-aids)
  ├── dispatch()  — move files between lanes, launch agents
  └── notify()    — Telegram alerts for human decisions + idle warnings
```

### Three Agents

| Agent | Role | Responsibility |
|-------|------|---------------|
| **OpenClaw** | Control Plane | Routes tasks, makes scheduling decisions, never writes code |
| **Codex** | Execution Plane | Sole code/content producer, works in 15-45 min slices |
| **Claude Code** | Supervision Plane | Audits quality, verifies deliverables, approves or returns work |

### Task Lifecycle

```
backlog → active/ → awaiting_audit/ → done/
           ↑              ↓
           └── rework ────┘

           ↕
      awaiting_human/ (auto-deescalates after 10 min)
```

Lane = state. When owner changes, the file physically moves.

## Multi-Project Support

One engine manages all projects. Each project has its own:
- Task queue (`.ai/tasks/`)
- Backlog (`project.json`)
- Dashboard (independent port)
- Agent quota

```
~/.local/lib/3body/
├── engine.py              ← shared engine
├── projects.json          ← project registry
├── filelock.py
└── telegram.py

project-a/
├── project.json           ← config + backlog
├── .ai/tasks/{active,awaiting_audit,awaiting_human,blocked,done}/
└── web/                   ← dashboard

project-b/
├── project.json
├── .ai/tasks/
└── web/
```

## Quick Start

### 1. Install engine

```bash
mkdir -p ~/.local/lib/3body
cp engine/*.py ~/.local/lib/3body/

mkdir -p ~/.local/bin
cp scripts/3body-engine.sh ~/.local/bin/
chmod +x ~/.local/bin/3body-engine.sh
```

### 2. Create project registry

```bash
cp projects.json.example ~/.local/lib/3body/projects.json
# Edit: set your project paths
```

### 3. Create a project

```bash
mkdir -p ~/my-project/.ai/tasks/{active,awaiting_audit,awaiting_human,blocked,done}
cp project.json.example ~/my-project/project.json
# Edit: set project_id, backlog tasks, dashboard port
```

### 4. Register the project

Add to `~/.local/lib/3body/projects.json`:
```json
{
  "projects": [
    {"id": "my-project", "root": "/Users/you/my-project", "enabled": true}
  ]
}
```

### 5. Install launchd service (macOS)

```bash
cp launchd/com.3body.engine.plist.example ~/Library/LaunchAgents/com.3body.engine.plist
# Edit: update paths
launchctl load ~/Library/LaunchAgents/com.3body.engine.plist
```

### 6. Install Claude Code skill (optional)

```bash
mkdir -p ~/.claude/plugins/marketplaces/claude-plugins-official/plugins/3body/skills/3body
cp skill/SKILL.md ~/.claude/plugins/marketplaces/claude-plugins-official/plugins/3body/skills/3body/
cp skill/manifest.json ~/.claude/plugins/marketplaces/claude-plugins-official/plugins/3body/
```

Then use `/3body status`, `/3body efficiency 3h`, `/3body audit` in Claude Code.

## Configuration

### project.json

```json
{
  "project_id": "my-project",
  "project_name": "My Project",
  "max_codex": 2,
  "max_claude_code": 2,
  "min_active_tasks": 4,
  "dashboard_port": 8765,
  "backlog": [
    {"id": "task-1", "goal": "Do something specific", "type": "execution", "priority": "p1"}
  ]
}
```

### Agent Budget

- Global limit: `max_total_agents` in `projects.json` (default: 8)
- Per-project: `max_codex` + `max_claude_code`
- Fair allocation: proportional split when global budget is scarce

### Anti-Ping-Pong

- 3-minute cooldown: same agent won't re-dispatch to same task within 3 min
- 5 dispatches in 10 min = auto-archive as done
- Reap waits 1 cycle before re-dispatch

## Agent Contracts

See `templates/` for the RACI contracts:
- `openclaw-controller.md` — routing rules, no-idle contract
- `codex-executor.md` — execution boundaries, checkpoint format
- `claude-completion-audit.md` — audit criteria, pass/rework/escalate
- `claude-goal-alignment.md` — alignment checks
- `claude-progress-check.md` — progress intervention rules

## Requirements

- macOS (for launchd; adaptable to systemd/cron)
- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) (`codex exec`)
- [Claude Code CLI](https://claude.com/claude-code) (`claude --print`)
- Telegram bot (optional, for notifications)

## License

MIT
