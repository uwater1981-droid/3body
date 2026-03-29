#!/usr/bin/env python3
"""
3body Engine — unified control loop.

Replaces: openclaw_patrol.py, watchdog.py, autopilot, human-decision-notify.
Runs every 60s via launchd. Pipeline: scan → decide → dispatch → notify.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────
# Default config — overridden by project.json per project
DEFAULT_CONFIG = {
    'human_timeout_minutes': 10,
    'openclaw_stale_minutes': 5,
    'max_codex': 2,
    'max_claude_code': 2,
    'min_active_tasks': 4,
}

REGISTRY_PATH = Path(__file__).resolve().parent / 'projects.json'

LANES = ['active', 'awaiting_audit', 'awaiting_human', 'blocked', 'done']
LANE_FOR_OWNER = {
    'codex': 'active',
    'openclaw': 'active',
    'claude_code': 'awaiting_audit',
    'human': 'awaiting_human',
}
TZ = timezone(timedelta(hours=8))

# ── Imports from co-located modules ──────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from filelock import read_json_locked, write_json_locked

# ── Utilities ────────────────────────────────────────────────────────────

def now_local():
    return datetime.now(TZ)

def now_iso():
    return now_local().isoformat()

def parse_ts(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(TZ)
    except Exception:
        return None

def is_pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def pick_latest_checkpoint(data: dict) -> dict:
    cps = data.get('checkpoints') or []
    if not cps:
        return {}
    return max(cps, key=lambda c: c.get('timestamp', ''))

def has_passing_audit(data: dict) -> bool:
    for a in (data.get('completion_audits') or []):
        if (a.get('decision') or '').lower() in ('pass', 'audited_pass'):
            return True
    charter = data.get('task_charter') or {}
    return charter.get('status', '') in ('done', 'completed', 'audited_pass', 'audited_conditional_pass')

def compute_human_wait_start(data: dict) -> datetime | None:
    """Find when owner was last set to 'human' from checkpoint timestamps."""
    for cp in reversed(data.get('checkpoints') or []):
        if cp.get('next_owner') == 'human':
            return parse_ts(cp.get('timestamp'))
    charter = data.get('task_charter') or {}
    if charter.get('owner') == 'human':
        return parse_ts(charter.get('updated_at'))
    return None

def minimal_checkpoint(agent, phase, findings, next_owner, next_step):
    now = now_iso()
    return {
        'checkpoint_id': f"engine-{now.replace(':', '').replace('-', '')}",
        'timestamp': now,
        'agent': agent,
        'phase': phase,
        'status': 'on_track',
        'changes_or_findings': findings if isinstance(findings, list) else [findings],
        'evidence': [],
        'risks': [],
        'next_owner': next_owner,
        'blocked_on': [],
        'acceptance_progress': [],
        'next_step': next_step,
        'needs_clarification': False,
        'changed_paths': [],
        'substantive_update': True,
        'alignment_decision': 'approve',
    }


# ── Project Config ────────────────────────────────────────────────────────

def load_projects_registry() -> dict:
    """Load the global projects registry."""
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'global': {'max_total_agents': 8}, 'projects': []}


def load_project_config(root: Path) -> dict:
    """Load per-project config from project.json, with defaults."""
    config = dict(DEFAULT_CONFIG)
    pj = root / 'project.json'
    if pj.exists():
        try:
            data = json.loads(pj.read_text(encoding='utf-8'))
            config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
            config['backlog'] = data.get('backlog', [])
            config['project_id'] = data.get('project_id', root.name)
            config['project_name'] = data.get('project_name', root.name)
        except Exception:
            config['backlog'] = []
            config['project_id'] = root.name
            config['project_name'] = root.name
    else:
        config['backlog'] = []
        config['project_id'] = root.name
        config['project_name'] = root.name
    return config


def count_all_running(registry: dict) -> int:
    """Count total running agent processes across all projects."""
    total = 0
    for proj in registry.get('projects', []):
        if not proj.get('enabled', True):
            continue
        root = Path(proj['root'])
        task_root = root / '.ai' / 'tasks'
        if not task_root.exists():
            continue
        for lane in ['active', 'awaiting_audit']:  # Only running agents in these lanes
            lane_dir = task_root / lane
            if not lane_dir.exists():
                continue
            for p in lane_dir.glob('*.json'):
                data = read_json_locked(p)
                if not data or '__error__' in data:
                    continue
                rt = (data.get('task_charter', {}).get('runtime') or {})
                if rt.get('status') == 'running' and is_pid_alive(rt.get('pid')):
                    total += 1
    return total


# ── Data Classes ─────────────────────────────────────────────────────────

@dataclass
class TaskState:
    task_id: str
    path: Path
    lane: str
    data: dict
    owner: str
    status: str
    runtime_agent: str
    runtime_status: str       # running | stopped | completed | ''
    runtime_pid: int | None
    pid_alive: bool
    updated_at: datetime | None
    is_done: bool
    latest_cp: dict
    human_wait_start: datetime | None

    @property
    def age_since_human_wait(self) -> float | None:
        if not self.human_wait_start:
            return None
        return (now_local() - self.human_wait_start).total_seconds() / 60

@dataclass
class Action:
    task: TaskState
    action_type: str          # move | launch | reap | notify_human | archive | noop
    target_owner: str
    target_lane: str
    agent_to_launch: str | None
    reason: str


# ── Stage 1: Scan ────────────────────────────────────────────────────────

def scan(root: Path) -> list[TaskState]:
    """Read all task files, check PID liveness, return task states."""
    task_root = root / '.ai' / 'tasks'
    tasks = []
    for lane in LANES:
        lane_dir = task_root / lane
        if not lane_dir.exists():
            continue
        for p in sorted(lane_dir.glob('*.json')):
            data = read_json_locked(p)
            if not data or '__error__' in data:
                continue
            charter = data.get('task_charter') or {}
            tid = charter.get('task_id') or p.stem
            if 'demo' in tid:
                continue
            rt = charter.get('runtime') or {}
            pid = rt.get('pid')
            alive = is_pid_alive(pid) if rt.get('status') == 'running' else False
            latest_cp = pick_latest_checkpoint(data)

            tasks.append(TaskState(
                task_id=tid,
                path=p,
                lane=lane,
                data=data,
                owner=(charter.get('owner') or '').strip(),
                status=(charter.get('status') or '').strip(),
                runtime_agent=(rt.get('agent') or ''),
                runtime_status=(rt.get('status') or ''),
                runtime_pid=int(pid) if pid else None,
                pid_alive=alive,
                updated_at=parse_ts(charter.get('updated_at', '')),
                is_done=has_passing_audit(data),
                latest_cp=latest_cp,
                human_wait_start=compute_human_wait_start(data),
            ))
    return tasks


# ── Stage 2: Decide ──────────────────────────────────────────────────────

def count_recent_dispatches(task: TaskState, minutes: int = 10) -> int:
    """Count how many times this task was dispatched in the last N minutes."""
    cutoff = now_local() - timedelta(minutes=minutes)
    count = 0
    for cp in (task.data.get('checkpoints') or []):
        ts = parse_ts(cp.get('timestamp', ''))
        if not ts or ts < cutoff:
            continue
        findings = ' '.join(cp.get('changes_or_findings', [])).lower()
        if 'launched' in findings or 'dispatched' in findings:
            count += 1
    return count


def route(task: TaskState, config: dict = None) -> str:
    """Pure function: given task state, return the correct owner."""
    cfg = config or DEFAULT_CONFIG
    # 0. Anti-ping-pong: force done if dispatched too many times
    if count_recent_dispatches(task, 10) >= 5 or count_recent_dispatches(task, 30) >= 8:
        return 'archive'

    # 1. Done → archive
    if task.is_done:
        return 'archive'

    # 2. Dead process → pipeline handoff (not just redispatch!)
    if task.runtime_status == 'running' and not task.pid_alive:
        finished_agent = task.runtime_agent or 'codex'
        cp_next = (task.latest_cp.get('next_owner') or '').strip()
        cp_status = (task.latest_cp.get('status') or '').strip()

        # Pipeline: Codex finished → Claude Code audits
        if finished_agent == 'codex':
            return 'claude_code'
        # Pipeline: Claude Code finished → check result
        if finished_agent == 'claude_code':
            if cp_status in ('done', 'pass', 'audited_pass'):
                return 'archive'
            if cp_next == 'codex':
                return 'codex'
            return 'codex'
        return finished_agent

    # 3. Needs audit → claude_code
    if task.status == 'awaiting_audit' or task.lane == 'awaiting_audit':
        return 'claude_code'

    # 4. Human with timeout → openclaw auto-decides
    if task.owner == 'human':
        wait = task.age_since_human_wait
        if wait is not None and wait >= cfg['human_timeout_minutes']:
            return 'openclaw'
        return 'human'

    # 5. Follow checkpoint's next_owner recommendation
    cp_next = (task.latest_cp.get('next_owner') or '').strip()
    if cp_next in ('codex', 'claude_code') and cp_next != 'human':
        return cp_next

    # 6. Executing tasks without running agent → codex
    if task.status == 'executing' and not task.pid_alive:
        return 'codex'

    # 7. Default
    return task.owner or 'openclaw'


def lane_for(owner: str, task: TaskState) -> str:
    if owner == 'archive':
        return 'done'
    # When deescalating from human → openclaw, go to active (not blocked)
    if task.owner == 'human' and owner == 'openclaw':
        return 'active'
    if task.status == 'blocked' and owner == 'openclaw' and task.lane == 'blocked':
        return 'blocked'
    return LANE_FOR_OWNER.get(owner, 'active')


def decide(tasks: list[TaskState], config: dict = None) -> list[Action]:
    """Determine actions for all tasks + agent dispatches."""
    cfg = config or DEFAULT_CONFIG
    actions = []
    running_agents = {t.runtime_agent for t in tasks if t.pid_alive}

    for task in tasks:
        target_owner = route(task, cfg)
        target_lane = lane_for(target_owner, task)

        # Dead process → reap
        if task.runtime_status == 'running' and not task.pid_alive:
            actions.append(Action(task, 'reap', target_owner, target_lane, None,
                                  f'Dead process pid={task.runtime_pid}'))
            continue

        # Archive done tasks
        if target_owner == 'archive' and task.lane != 'done':
            actions.append(Action(task, 'archive', 'openclaw', 'done', None, 'Task done'))
            continue

        # Lane mismatch → move
        if target_lane != task.lane:
            actions.append(Action(task, 'move', target_owner, target_lane, None,
                                  f'{task.owner}→{target_owner}'))
            continue

        # Owner mismatch (same lane) → update owner
        if target_owner not in ('archive',) and target_owner != task.owner:
            actions.append(Action(task, 'move', target_owner, target_lane, None,
                                  f'owner: {task.owner}→{target_owner}'))
            continue

        actions.append(Action(task, 'noop', target_owner, target_lane, None, ''))

    # Dispatch agents — allow multiple concurrent instances per agent type
    # Cooldown: per-agent — only block re-dispatch to the SAME agent within 3 min
    cooldown_cutoff = now_local() - timedelta(minutes=3)
    cooled_agent_task = set()  # set of (agent, task_id) pairs
    for t in tasks:
        if not t.updated_at or t.updated_at <= cooldown_cutoff:
            continue
        for cp in reversed(t.data.get('checkpoints') or []):
            ts = parse_ts(cp.get('timestamp', ''))
            if not ts or ts < cooldown_cutoff:
                break
            findings = ' '.join(cp.get('changes_or_findings', [])).lower()
            if 'launched' in findings:
                for a in ['codex', 'claude_code']:
                    if a in findings:
                        cooled_agent_task.add((a, t.task_id))

    launched_ids = {a.task.task_id for a in actions if a.action_type == 'launch'}
    reaped_ids = {a.task.task_id for a in actions if a.action_type == 'reap'}  # Don't re-launch in same cycle
    moving_to_active = {a.task.task_id for a in actions if a.target_lane == 'active'}
    moving_to_audit = {a.task.task_id for a in actions if a.target_lane == 'awaiting_audit'}

    # Count currently running per agent type
    running_count = {}
    for t in tasks:
        if t.pid_alive:
            running_count[t.runtime_agent] = running_count.get(t.runtime_agent, 0) + 1

    # Claude Code: prefer awaiting_audit, then active — fill up to max_claude_code
    cc_running = running_count.get('claude_code', 0)
    cc_slots = cfg['max_claude_code'] - cc_running
    if cc_slots > 0:
        candidates = sorted(
            [t for t in tasks if not t.is_done and t.owner != 'human'
             and not t.pid_alive and t.task_id not in launched_ids
             and t.task_id not in reaped_ids
             and ('claude_code', t.task_id) not in cooled_agent_task],
            key=lambda t: (0 if (t.lane == 'awaiting_audit' or t.task_id in moving_to_audit) else 1,
                           t.updated_at or datetime.min.replace(tzinfo=TZ))
        )
        for t in candidates:
            if cc_slots <= 0:
                break
            effective_lane = t.lane
            if t.task_id in moving_to_audit:
                effective_lane = 'awaiting_audit'
            elif t.task_id in moving_to_active:
                effective_lane = 'active'
            if effective_lane in ('awaiting_audit', 'active'):
                actions.append(Action(t, 'launch', 'claude_code', effective_lane, 'claude_code',
                                      'Idle claude_code dispatched'))
                launched_ids.add(t.task_id)
                cc_slots -= 1

    # Codex: active tasks — fill up to max_codex
    codex_running = running_count.get('codex', 0)
    codex_slots = cfg['max_codex'] - codex_running
    if codex_slots > 0:
        for t in tasks:
            if codex_slots <= 0:
                break
            if t.task_id in launched_ids or t.task_id in reaped_ids or ('codex', t.task_id) in cooled_agent_task or t.is_done or t.owner == 'human':
                continue
            if t.pid_alive:
                continue
            if t.lane != 'active' and t.task_id not in moving_to_active:
                continue
            cp_next = (t.latest_cp.get('next_owner') or '').strip()
            if t.owner in ('codex', 'openclaw') or cp_next == 'codex':
                actions.append(Action(t, 'launch', 'codex', 'active', 'codex',
                                      'Idle codex dispatched'))
                launched_ids.add(t.task_id)
                codex_slots -= 1

    return actions


# ── Stage 3: Dispatch ────────────────────────────────────────────────────

def execute_reap(task: TaskState, root: Path):
    """Mark dead runtime as stopped, add checkpoint."""
    data = task.data
    charter = data['task_charter']
    rt = charter.get('runtime') or {}
    now = now_iso()
    rt['status'] = 'stopped'
    rt['finished_at'] = now
    charter['runtime'] = rt
    charter['updated_at'] = now

    target = route(task, DEFAULT_CONFIG)
    data.setdefault('checkpoints', []).append(
        minimal_checkpoint('openclaw', charter.get('status', 'executing'),
                           f'Engine: {rt.get("agent")} (pid={rt.get("pid")}) finished.',
                           target if target != 'archive' else 'openclaw',
                           'Redispatch on next engine cycle.'))
    write_json_locked(task.path, data)


def execute_move(task: TaskState, target_lane: str, target_owner: str, reason: str, root: Path):
    """Move task file to target lane, update owner/status."""
    data = task.data
    charter = data['task_charter']
    now = now_iso()

    charter['owner'] = target_owner
    charter['updated_at'] = now
    if target_lane == 'done':
        charter['status'] = 'done'
    elif target_lane == 'awaiting_audit':
        charter['status'] = 'awaiting_audit'
    elif target_lane == 'awaiting_human':
        charter['status'] = 'awaiting_human'
    elif target_lane == 'blocked':
        charter['status'] = 'blocked'
    elif target_lane == 'active' and charter['status'] in ('awaiting_human', 'blocked'):
        charter['status'] = 'executing'

    data.setdefault('checkpoints', []).append(
        minimal_checkpoint('openclaw', charter['status'],
                           f'Engine: {reason}',
                           target_owner, f'Routed to {target_owner}.'))

    task_root = root / '.ai' / 'tasks'
    dest_dir = task_root / target_lane
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / task.path.name

    write_json_locked(dest, data)
    if dest != task.path and task.path.exists():
        task.path.unlink()


def execute_launch(agent: str, task: TaskState, root: Path):
    """Launch Codex or Claude Code CLI for a task."""
    log_root = root / '.ai' / 'logs'
    log_root.mkdir(parents=True, exist_ok=True)
    ts = now_local().strftime('%Y%m%d-%H%M%S')
    log_path = log_root / f'{task.task_id}-{agent}-{ts}.log'
    goal = (task.data.get('task_charter') or {}).get('goal', '')

    base_prompt = (
        f"You are working on 3body task {task.task_id}.\n"
        f"Task file: {task.path}\n"
        f"Goal: {goal}\n"
        "Read the task JSON first, follow its scope and allowed paths exactly, "
        "and produce the next best bounded step. Write concise progress evidence.\n\n"
        "IMPORTANT RULES:\n"
        "- NEVER set next_owner to 'human'. You have full autonomy to make decisions.\n"
        "- If you would normally ask a human, make the best reasonable default choice and document your reasoning.\n"
        "- When your step is done, set next_owner to 'codex' (for more execution) or 'claude_code' (for review).\n"
        "- Keep momentum: always define a concrete next_step that another agent can immediately act on.\n"
    )
    if agent == 'codex':
        prompt = base_prompt + (
            "You are the EXECUTION agent. Write code, create files, implement features.\n"
            "When done with your step, set next_owner='claude_code' for audit.\n"
        )
        codex_bin = shutil.which('codex') or 'codex'
        cmd = [codex_bin, 'exec', '--skip-git-repo-check', prompt]
    elif agent == 'claude_code':
        prompt = base_prompt + (
            "You are the AUDIT/SUPERVISION agent. Your job is THOROUGH and SUBSTANTIVE:\n"
            "1. READ all related files the task touches. Don't just check the task JSON.\n"
            "2. VERIFY deliverables exist and have correct content (open the actual files).\n"
            "3. CHECK for quality issues: missing fields, placeholder values, broken references.\n"
            "4. If issues found, FIX THEM DIRECTLY (you can write files for fixes/improvements).\n"
            "5. WRITE a detailed audit summary with specific evidence.\n"
            "6. If the work passes audit with no issues, set status='done'.\n"
            "7. If you fixed issues or more work needed, set next_owner='codex' with specific instructions.\n"
            "Take your time. A thorough 5-minute review is worth more than a 30-second rubber stamp.\n"
        )
        claude_bin = shutil.which('claude') or 'claude'
        cmd = [claude_bin, '--print', '--dangerously-skip-permissions',
               '--model', 'opus', '--effort', 'max', prompt]
    else:
        return None

    with log_path.open('ab') as f:
        # Fix PATH to include /usr/local/bin for node/npm access
        env = os.environ.copy()
        path_env = env.get('PATH', '')
        if '/usr/local/bin' not in path_env:
            env['PATH'] = '/usr/local/bin:' + path_env
        proc = subprocess.Popen(
            cmd, cwd=str(root), stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env,
        )

    # Update task file
    data = task.data
    charter = data['task_charter']
    now = now_iso()
    charter['runtime'] = {
        'agent': agent,
        'status': 'running',
        'pid': proc.pid,
        'started_at': now,
        'finished_at': '',
        'log_path': str(log_path),
    }
    charter['owner'] = agent
    charter['updated_at'] = now
    data.setdefault('checkpoints', []).append(
        minimal_checkpoint('openclaw', 'executing',
                           f'Engine: launched {agent} (pid={proc.pid}).',
                           agent, f'{agent} is running.'))
    write_json_locked(task.path, data)
    return proc.pid


def dispatch(actions: list[Action], root: Path) -> list[dict]:
    """Execute all actions. Returns results for notification."""
    results = []
    for a in actions:
        if a.action_type == 'noop':
            continue
        try:
            if a.action_type == 'reap':
                execute_reap(a.task, root)
                results.append({'type': 'reaped', 'task': a.task.task_id,
                                'agent': a.task.runtime_agent})
            elif a.action_type in ('move', 'archive'):
                execute_move(a.task, a.target_lane, a.target_owner, a.reason, root)
                results.append({'type': 'moved', 'task': a.task.task_id,
                                'to': a.target_lane, 'owner': a.target_owner})
            elif a.action_type == 'launch':
                pid = execute_launch(a.agent_to_launch, a.task, root)
                if pid:
                    results.append({'type': 'launched', 'task': a.task.task_id,
                                    'agent': a.agent_to_launch, 'pid': pid})
        except Exception as e:
            print(f'  [ERROR] {a.action_type} on {a.task.task_id}: {e}')
    return results


# ── Stage 4: Notify ──────────────────────────────────────────────────────

def notify(results: list[dict], tasks: list[TaskState]):
    """Send Telegram for human-needed moves and idle alerts."""
    from telegram import send_message, format_human_decision_request

    # Notify human for tasks moved to awaiting_human
    for r in results:
        if r['type'] == 'moved' and r['to'] == 'awaiting_human':
            tid = r['task']
            task = next((t for t in tasks if t.task_id == tid), None)
            if task:
                cp = task.latest_cp
                msg = format_human_decision_request(
                    tid, task.data.get('task_charter', {}).get('goal', ''),
                    cp.get('next_step', ''),
                    cp.get('changes_or_findings', []),
                    cp.get('risks', []),
                    cp.get('blocked_on', []),
                )
                ok = send_message(msg)
                print(f'  Telegram {"sent" if ok else "FAILED"} for {tid}')

    # Idle alert: no running agents and no launches this cycle
    launched = any(r['type'] == 'launched' for r in results)
    has_running = any(t.pid_alive for t in tasks)
    has_work = any(t.lane in ('active', 'awaiting_audit') and not t.is_done
                   and t.owner != 'human' for t in tasks)

    if not launched and not has_running and not has_work:
        # Cooldown: max one idle alert per 60 minutes, per-project
        pid = getattr(notify, '_project_id', 'default')
        idle_flag = Path(f'/tmp/.3body-idle-{pid}')
        should_alert = True
        if idle_flag.exists():
            try:
                age = (now_local() - datetime.fromtimestamp(idle_flag.stat().st_mtime, tz=TZ)).total_seconds()
                if age < 3600:  # 1 hour cooldown
                    should_alert = False
            except Exception:
                pass
        if should_alert:
            send_message(f"⚠️ 3body [{pid}] 空闲 — backlog 已耗尽，没有运行中的 agent。")
            idle_flag.touch()
            print('  Telegram: idle alert sent')
    else:
        pid = getattr(notify, '_project_id', 'default')
        Path(f'/tmp/.3body-idle-{pid}').unlink(missing_ok=True)


# ── Stage 5: Task Generation ─────────────────────────────────────────────

def generate_tasks_if_needed(root: Path, tasks: list[TaskState], config: dict = None) -> list[str]:
    """Create new tasks from backlog if active queue is too short."""
    cfg = config or DEFAULT_CONFIG
    backlog = cfg.get('backlog', [])
    min_active = cfg.get('min_active_tasks', 4)
    task_root = root / '.ai' / 'tasks'

    # Count non-done, non-human tasks
    workable = [t for t in tasks if t.lane in ('active', 'awaiting_audit')
                and not t.is_done and t.owner != 'human']

    if len(workable) >= min_active:
        return []

    # Find existing task IDs (all lanes)
    existing_ids = {t.task_id for t in tasks}

    # Also check by suffix (task IDs have date prefix)
    existing_suffixes = set()
    for tid in existing_ids:
        parts = tid.split('-', 3)
        if len(parts) >= 4:
            existing_suffixes.add(parts[3])

    created = []
    today = now_local().strftime('%Y-%m-%d')

    for item in backlog:
        if not isinstance(item, dict):
            continue  # Skip invalid entries
        suffix = item.get('id', '')
        goal = item.get('goal', '')
        task_type = item.get('type', 'execution')
        priority = item.get('priority', 'p2')
        if not suffix or not goal:
            continue

        if len(workable) + len(created) >= min_active:
            break
        if suffix in existing_suffixes:
            continue

        task_id = f'{today}-{suffix}'
        if task_id in existing_ids:
            continue

        now = now_iso()
        data = {
            'version': '1.1',
            'task_charter': {
                'task_id': task_id,
                'repo_path': str(root),
                'goal': goal,
                'non_goals': [],
                'allowed_paths': ['.ai/tasks', 'docs', 'assets', 'scripts'],
                'acceptance_checks': [
                    'Task produces concrete deliverables (files, configs, or published content)',
                    'Progress evidence is written back to task checkpoint',
                ],
                'known_facts': [],
                'open_questions': [],
                'owner': 'codex',
                'status': 'executing',
                'task_type': task_type,
                'priority': priority,
                'decision_owner': 'openclaw',
                'timebox_minutes': 45,
                'created_at': now,
                'updated_at': now,
            },
            'checkpoints': [{
                'checkpoint_id': f"engine-gen-{now.replace(':', '').replace('-', '')}",
                'timestamp': now,
                'agent': 'openclaw',
                'phase': 'executing',
                'status': 'on_track',
                'changes_or_findings': [f'Engine auto-generated task from backlog: {goal[:80]}'],
                'evidence': [{'kind': 'observed', 'statement': 'Task generated by engine to maintain queue depth.', 'source': 'engine.py::generate_tasks_if_needed'}],
                'risks': [],
                'next_owner': 'codex',
                'blocked_on': [],
                'acceptance_progress': [],
                'next_step': 'Codex should read the goal and execute the first bounded step.',
                'needs_clarification': False,
                'changed_paths': [],
                'substantive_update': True,
                'alignment_decision': 'approve',
            }],
            'completion_audits': [],
        }

        dest = task_root / 'active' / f'{task_id}.json'
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_json_locked(dest, data)
        created.append(task_id)
        print(f'  Generated: {task_id}')

    return created


# ── Main ─────────────────────────────────────────────────────────────────

def run_project(root: Path, config: dict, dry_run: bool = False, total_running: int = 0, max_total: int = 8):
    """Run the engine pipeline for a single project."""
    pid = config.get('project_id', root.name)

    if not (root / '.ai' / 'tasks').exists():
        print(f'[{pid}] SKIP — no .ai/tasks/')
        return 0

    # Cap per-project agents by global remaining budget (fair split)
    remaining = max(0, max_total - total_running)
    proj_codex = config.get('max_codex', 2)
    proj_cc = config.get('max_claude_code', 2)
    proj_total = proj_codex + proj_cc
    if proj_total > remaining:
        # Fair split: proportional to configured max
        ratio = remaining / proj_total if proj_total > 0 else 0
        config['max_codex'] = max(0, int(proj_codex * ratio))
        config['max_claude_code'] = max(0, remaining - config['max_codex'])
    else:
        config['max_codex'] = proj_codex
        config['max_claude_code'] = proj_cc

    tasks = scan(root)

    if not dry_run:
        generated = generate_tasks_if_needed(root, tasks, config)
        if generated:
            tasks = scan(root)

    actions = decide(tasks, config)

    non_noop = [a for a in actions if a.action_type != 'noop']
    if non_noop:
        print(f'[{pid}] {len(non_noop)} action(s)')
        for a in non_noop:
            agent_info = f' → {a.agent_to_launch}' if a.agent_to_launch else ''
            print(f'  {a.action_type:8s} {a.task.task_id[:50]:50s} {a.reason}{agent_info}')
    else:
        running = [t for t in tasks if t.pid_alive]
        if running:
            agents = ', '.join(f'{t.runtime_agent}({t.task_id[:25]})' for t in running)
            print(f'[{pid}] OK — {agents}')

    if dry_run:
        return 0

    results = dispatch(actions, root)
    notify._project_id = pid  # Set project context for idle alerts
    notify(results, tasks)
    return sum(1 for r in results if r['type'] == 'launched')


def main():
    parser = argparse.ArgumentParser(description='3body Engine — unified multi-project control loop.')
    parser.add_argument('--root', default=None,
                        help='Single project root (bypasses projects.json)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print actions without executing')
    args = parser.parse_args()

    # Single-project mode (backward compatible)
    if args.root:
        root = Path(args.root).expanduser().resolve()
        config = load_project_config(root)
        run_project(root, config, args.dry_run)
        if args.dry_run:
            print('[DRY RUN] No changes made.')
        return

    # Multi-project mode: iterate over all registered projects
    registry = load_projects_registry()
    global_config = registry.get('global', {})
    max_total = global_config.get('max_total_agents', 8)

    total_running = count_all_running(registry)

    for proj in registry.get('projects', []):
        if not proj.get('enabled', True):
            continue
        root = Path(proj['root']).expanduser().resolve()
        config = load_project_config(root)
        launched = run_project(root, config, args.dry_run, total_running, max_total)
        total_running += launched

    if args.dry_run:
        print('[DRY RUN] No changes made.')


if __name__ == '__main__':
    main()
