#!/usr/bin/env python3
"""3body Dashboard Server v2.0 — shared across all projects."""
import argparse
import json
import socket
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
import os
import sys

API_VERSION = '2.0'

# Resolve ROOT: --root CLI arg > THREEBODY_ROOT env > script parent
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument('--root', default=os.environ.get(
    'THREEBODY_ROOT', str(Path(__file__).resolve().parents[1])))
_args, _ = _parser.parse_known_args()
ROOT = Path(_args.root).resolve()

# Shared lib directory (where this script lives)
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT / 'scripts'))
from filelock import write_json_locked

# Web directory: prefer project-local, fallback to shared lib
WEB = ROOT / 'web' if (ROOT / 'web').exists() else SCRIPT_DIR / 'web'
TASK_ROOT = Path(os.environ.get('THREEBODY_TASK_ROOT', str(ROOT / '.ai' / 'tasks')))
LOG_ROOT = ROOT / '.ai' / 'logs'
ORDER = ['active', 'awaiting_audit', 'awaiting_human', 'blocked', 'done']
DISPLAY = {
    'active': 'ACTIVE',
    'awaiting_audit': 'AUDIT',
    'awaiting_human': 'HUMAN',
    'blocked': 'BLOCKED',
    'done': 'DONE',
}
STATUS_BY_LANE = {
    'active': 'executing',
    'awaiting_audit': 'awaiting_audit',
    'awaiting_human': 'awaiting_human',
    'blocked': 'blocked',
    'done': 'done',
}


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        return {'__error__': str(e)}


def write_json(path: Path, data: dict):
    write_json_locked(path, data)


def is_pid_running(pid):
    """Check if a process is alive (read-only, used for display only)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def pick_latest(task: dict):
    checkpoints = task.get('checkpoints') or []
    audits = task.get('completion_audits') or []
    items = []
    for cp in checkpoints:
        items.append({
            'type': 'checkpoint',
            'timestamp': cp.get('timestamp', ''),
            'agent': cp.get('agent', ''),
            'status': cp.get('status', ''),
            'next_owner': cp.get('next_owner', ''),
            'next_step': cp.get('next_step', ''),
            'summary': '; '.join(cp.get('changes_or_findings') or [])[:300],
        })
    for ad in audits:
        items.append({
            'type': 'audit',
            'timestamp': ad.get('timestamp', ''),
            'agent': ad.get('agent', ''),
            'status': ad.get('decision', ''),
            'next_owner': '',
            'next_step': ad.get('summary', ''),
            'summary': ad.get('summary', '')[:300],
        })
    items.sort(key=lambda x: x.get('timestamp', ''))
    return items[-1] if items else None


def collect():
    rows = []
    counts = {k: 0 for k in ORDER}
    for dirname in ORDER:
        d = TASK_ROOT / dirname
        if not d.exists():
            continue
        for path in sorted(d.glob('*.json')):
            data = load_json(path)
            counts[dirname] += 1
            if '__error__' in data:
                rows.append({
                    'lane': dirname,
                    'laneLabel': DISPLAY[dirname],
                    'task': path.name,
                    'status': 'parse_error',
                    'owner': '-',
                    'updated': '',
                    'nextOwner': '-',
                    'summary': data['__error__'],
                    'nextStep': '',
                    'blockedOn': [],
                    'path': str(path),
                })
                continue
            charter = data.get('task_charter') or {}
            latest = pick_latest(data) or {}
            cps = data.get('checkpoints') or []
            latest_cp = sorted(cps, key=lambda x: x.get('timestamp', ''))[-1] if cps else {}
            updated = charter.get('updated_at') or latest.get('timestamp', '')
            history = []
            for cp in sorted(cps, key=lambda x: x.get('timestamp', ''))[-10:]:
                history.append({
                    'type': 'checkpoint',
                    'timestamp': cp.get('timestamp', ''),
                    'agent': cp.get('agent', ''),
                    'status': cp.get('status', ''),
                    'summary': '; '.join(cp.get('changes_or_findings') or [])[:240],
                    'nextStep': cp.get('next_step', ''),
                    'blockedOn': cp.get('blocked_on', []) or [],
                })
            audits = data.get('completion_audits') or []
            for ad in sorted(audits, key=lambda x: x.get('timestamp', ''))[-5:]:
                history.append({
                    'type': 'audit',
                    'timestamp': ad.get('timestamp', ''),
                    'agent': ad.get('agent', ''),
                    'status': ad.get('decision', ''),
                    'summary': ad.get('summary', '')[:240],
                    'nextStep': '',
                    'blockedOn': [],
                })
            history.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            runtime = charter.get('runtime') or {}
            effective_lane = dirname
            if (
                (charter.get('status') in ('audited_pass', 'done')) or
                (latest_cp.get('status') in ('pass', 'done'))
            ):
                effective_lane = 'done'
            elif dirname == 'active' and (
                (charter.get('status') == 'blocked') or
                (latest_cp.get('status') == 'blocked') or
                bool(latest_cp.get('blocked_on', []) or [])
            ):
                effective_lane = 'blocked'
            rows.append({
                'lane': effective_lane,
                'laneLabel': DISPLAY[effective_lane],
                'task': charter.get('task_id') or path.stem,
                'status': charter.get('status') or dirname,
                'owner': charter.get('owner') or '-',
                'updated': updated,
                'nextOwner': latest.get('next_owner', ''),
                'summary': latest.get('summary') or charter.get('goal', ''),
                'nextStep': latest_cp.get('next_step', ''),
                'blockedOn': latest_cp.get('blocked_on', []) or [],
                'contentDraft': charter.get('panel_content', ''),
                'contentTitle': charter.get('panel_content_title', ''),
                'contentBody': charter.get('panel_content_body', ''),
                'contentTags': charter.get('panel_content_tags', []),
                'contentNotes': charter.get('panel_content_notes', ''),
                'humanDecision': charter.get('human_decision', None),
                'checkpointAgent': latest_cp.get('agent', ''),
                'checkpointStatus': latest_cp.get('status', ''),
                'runtime': {
                    'agent': runtime.get('agent', ''),
                    'status': runtime.get('status', ''),
                    'pid': runtime.get('pid'),
                    'startedAt': runtime.get('started_at', ''),
                    'finishedAt': runtime.get('finished_at', ''),
                    'logPath': runtime.get('log_path', ''),
                },
                'history': history,
                'path': str(path),
            })
    recent = sorted(rows, key=lambda x: x.get('updated', ''), reverse=True)[:8]
    summary = {
        'blockedCount': counts.get('blocked', 0),
        'activeCount': counts.get('active', 0),
        'doneCount': counts.get('done', 0),
        'recentUpdated': [
            {
                'task': r['task'],
                'lane': r['lane'],
                'updated': r['updated'],
                'summary': r['summary']
            } for r in recent
        ]
    }
    return {
        'apiVersion': API_VERSION,
        'generatedAt': datetime.now().astimezone().isoformat(),
        'root': str(ROOT),
        'counts': counts,
        'summary': summary,
        'tasks': rows,
    }


def find_task(task_id: str):
    for dirname in ORDER:
        d = TASK_ROOT / dirname
        if not d.exists():
            continue
        for path in d.glob('*.json'):
            data = load_json(path)
            charter = data.get('task_charter') or {}
            if charter.get('task_id') == task_id:
                return path, data, dirname
    return None, None, None




def compute_efficiency():
    """Compute agent efficiency based on actual runtime (started_at → finished_at).

    Efficiency = total agent-minutes running / wall-clock span.
    With N concurrent agents, max efficiency = N * 100%.
    We normalize to 0-100% by dividing by max possible concurrency.
    """
    from datetime import timedelta, timezone
    from collections import defaultdict
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)

    # ── Collect runtime intervals + checkpoints from all tasks ──
    all_cps = []
    run_intervals = []  # (agent, start_dt, end_dt, task_id)

    for dirname in ORDER:
        d = TASK_ROOT / dirname
        if not d.exists():
            continue
        for path in sorted(d.glob('*.json')):
            data = load_json(path)
            if '__error__' in data:
                continue
            charter = data.get('task_charter') or {}
            tid = charter.get('task_id') or path.stem
            if 'demo' in tid:
                continue

            # Checkpoints
            for cp in (data.get('checkpoints') or []):
                ts = cp.get('timestamp', '')
                try:
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(tz)
                    all_cps.append((dt, cp.get('agent', '?'), tid, cp))
                except Exception:
                    pass

            # Extract runtime intervals from checkpoint pairs: "launched" → next agent cp or "finished/stopped"
            cps_sorted = sorted(data.get('checkpoints') or [], key=lambda c: c.get('timestamp', ''))
            i = 0
            while i < len(cps_sorted):
                cp = cps_sorted[i]
                findings = ' '.join(cp.get('changes_or_findings', [])).lower()
                if 'launched' in findings or 'engine: launched' in findings:
                    # Find which agent
                    launched_agent = None
                    for a in ['codex', 'claude_code']:
                        if a in findings:
                            launched_agent = a
                            break
                    if launched_agent:
                        try:
                            start_dt = datetime.fromisoformat(cp['timestamp'].replace('Z', '+00:00')).astimezone(tz)
                        except Exception:
                            i += 1
                            continue
                        # Find end: next cp from same agent, or "finished"/"stopped"/"completed"
                        end_dt = None
                        for j in range(i + 1, len(cps_sorted)):
                            ncp = cps_sorted[j]
                            nf = ' '.join(ncp.get('changes_or_findings', [])).lower()
                            na = ncp.get('agent', '')
                            if na == launched_agent or 'finished' in nf or 'stopped' in nf or 'completed' in nf:
                                try:
                                    end_dt = datetime.fromisoformat(ncp['timestamp'].replace('Z', '+00:00')).astimezone(tz)
                                except Exception:
                                    pass
                                break
                        if not end_dt:
                            rt = charter.get('runtime') or {}
                            if rt.get('status') == 'running' and is_pid_running(rt.get('pid')):
                                end_dt = now
                            elif rt.get('finished_at'):
                                try:
                                    end_dt = datetime.fromisoformat(rt['finished_at'].replace('Z', '+00:00')).astimezone(tz)
                                except Exception:
                                    pass
                        if end_dt and end_dt > start_dt:
                            dur = (end_dt - start_dt).total_seconds() / 60
                            if dur < 180:  # Cap at 3 hours
                                run_intervals.append((launched_agent, start_dt, end_dt, tid))
                i += 1

    all_cps.sort(key=lambda x: x[0])

    # ── Currently running agents ──
    running = []
    for dirname in ['active', 'awaiting_audit']:
        d = TASK_ROOT / dirname
        if not d.exists():
            continue
        for path in d.glob('*.json'):
            data = load_json(path)
            if '__error__' in data:
                continue
            charter = data.get('task_charter') or {}
            rt = charter.get('runtime') or {}
            tid = charter.get('task_id') or path.stem
            if 'demo' in tid:
                continue
            if rt.get('pid') and is_pid_running(rt.get('pid')):
                started = rt.get('started_at', '')
                dur = 0
                try:
                    st = datetime.fromisoformat(started.replace('Z', '+00:00')).astimezone(tz)
                    dur = round((now - st).total_seconds() / 60)
                except Exception:
                    pass
                running.append({
                    'agent': rt.get('agent', '?'),
                    'task': tid,
                    'minutes': dur,
                    'pid': rt.get('pid'),
                })

    # ── Compute per-window stats ──
    windows = {
        '1h': timedelta(hours=1),
        '3h': timedelta(hours=3),
        '12h': timedelta(hours=12),
        'all': None,
    }
    max_agents = 6  # 3 codex + 3 claude_code

    result = {'windows': {}, 'running': running, 'generatedAt': now.isoformat()}

    for wname, delta in windows.items():
        cutoff = (now - delta) if delta else datetime.min.replace(tzinfo=tz)
        filtered_cps = [(dt, agent, tid, cp) for dt, agent, tid, cp in all_cps if dt >= cutoff]

        if len(filtered_cps) < 2:
            result['windows'][wname] = {
                'span_minutes': 0, 'total_cps': len(filtered_cps), 'efficiency': 0,
                'agents': {}, 'timeline': [],
            }
            continue

        span = (filtered_cps[-1][0] - filtered_cps[0][0]).total_seconds() / 60 or 1

        # ── Runtime-based efficiency ──
        # Clip intervals to window, sum agent-minutes
        agent_minutes = defaultdict(float)
        agent_runs = defaultdict(int)
        agent_tasks = defaultdict(set)

        for agent, start, end, tid in run_intervals:
            clipped_start = max(start, cutoff)
            clipped_end = min(end, now)
            if clipped_start >= clipped_end:
                continue
            dur = (clipped_end - clipped_start).total_seconds() / 60
            agent_minutes[agent] += dur
            agent_runs[agent] += 1
            agent_tasks[agent].add(tid)

        total_agent_minutes = sum(agent_minutes.values())
        # Efficiency = agent-minutes / (span * max_agents) * 100
        # This gives 100% when all 6 slots are always occupied
        efficiency = round(total_agent_minutes / (span * max_agents) * 100) if span > 0 else 0
        efficiency = min(efficiency, 100)
        idle_minutes = round(span * max_agents - total_agent_minutes)
        if idle_minutes < 0:
            idle_minutes = 0

        # ── Per-agent stats (checkpoint-based for rate, runtime-based for time) ──
        agents = {}
        for agent_name in ['openclaw', 'codex', 'claude_code']:
            substantive = 0
            dispatches = 0
            tasks_set = set()
            for dt, a, tid, cp in filtered_cps:
                if a != agent_name:
                    continue
                tasks_set.add(tid)
                findings = ' '.join(cp.get('changes_or_findings', [])).lower()
                if 'engine:' in findings or 'patrol' in findings:
                    pass
                elif 'launched' in findings or 'dispatched' in findings:
                    dispatches += 1
                else:
                    substantive += 1

            hours = span / 60 or 1
            agents[agent_name] = {
                'total': sum(1 for _, a, _, _ in filtered_cps if a == agent_name),
                'substantive': substantive,
                'dispatches': dispatches,
                'tasks': len(tasks_set) or len(agent_tasks.get(agent_name, set())),
                'rate': round(substantive / hours, 1),
                'run_minutes': round(agent_minutes.get(agent_name, 0)),
                'runs': agent_runs.get(agent_name, 0),
            }

        # ── Timeline buckets (10-min) ──
        buckets = defaultdict(lambda: {'openclaw': 0, 'codex': 0, 'claude_code': 0})
        for dt, agent, tid, cp in filtered_cps:
            key = dt.strftime('%H:%M')[:4] + '0'
            if agent in buckets[key]:
                buckets[key][agent] += 1
        timeline = [{'time': k, **v} for k, v in sorted(buckets.items())]

        result['windows'][wname] = {
            'span_minutes': round(span),
            'total_cps': len(filtered_cps),
            'efficiency': efficiency,
            'idle_minutes': idle_minutes,
            'total_agent_minutes': round(total_agent_minutes),
            'agents': agents,
            'timeline': timeline[-18:],
        }

    return result


def get_projects():
    """Return all registered projects from projects.json for the project tab bar."""
    registry_path = Path(os.path.expanduser('~/.local/lib/3body/projects.json'))
    projects = []
    if registry_path.exists():
        try:
            reg = json.loads(registry_path.read_text())
            for p in reg.get('projects', []):
                if not p.get('enabled', True):
                    continue
                # Port and name: prefer explicit values in registry, then fall back to project.json
                port = p.get('port', 8765)
                name = p.get('name', p['id'])
                if not port or port == 8765 and p['id'] != 'social':
                    root = Path(p['root'])
                    pj = root / 'project.json'
                    if pj.exists():
                        try:
                            cfg = json.loads(pj.read_text())
                            name = cfg.get('project_name', name)
                            port = cfg.get('dashboard_port', port)
                        except Exception:
                            pass
                # Count tasks — try local_task_root first, then iCloud root
                local_tr = p.get('local_task_root')
                task_root = Path(local_tr) if local_tr else Path(p['root']) / '.ai' / 'tasks'
                counts = {}
                for lane in ORDER:
                    d = task_root / lane
                    try:
                        counts[lane] = len(list(d.glob('*.json'))) if d.exists() else 0
                    except OSError:
                        counts[lane] = 0
                projects.append({
                    'id': p['id'],
                    'name': name,
                    'port': port,
                    'counts': counts,
                    'isCurrent': False,
                })
        except Exception:
            pass

    # Mark current project based on this server's own port
    my_port = int(os.environ.get('THREEBODY_PORT', '0')) or 8765
    if not os.environ.get('THREEBODY_PORT'):
        my_config = ROOT / 'project.json'
        if my_config.exists():
            try:
                my_port = json.loads(my_config.read_text()).get('dashboard_port', 8765)
            except Exception:
                pass
    for p in projects:
        p['isCurrent'] = (p['port'] == my_port)

    return {'projects': projects}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/tasks':
            return self._send_json(200, collect())
        if parsed.path == '/api/efficiency':
            return self._send_json(200, compute_efficiency())
        if parsed.path == '/api/projects':
            return self._send_json(200, get_projects())
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            return self._send_json(400, {'ok': False, 'error': 'invalid_json'})

        if parsed.path == '/api/update-task':
            task_id = payload.get('taskId', '').strip()
            summary = (payload.get('summary') or '').strip()
            next_step = (payload.get('nextStep') or '').strip()
            lane = (payload.get('lane') or '').strip()
            content_draft = payload.get('contentDraft') or ''
            content_title = (payload.get('contentTitle') or '').strip()
            content_body = payload.get('contentBody') or ''
            content_tags = payload.get('contentTags') or []
            if isinstance(content_tags, str):
                content_tags = [x.strip() for x in content_tags.split('\n') if x.strip()]
            content_notes = payload.get('contentNotes') or ''
            checkpoint_agent = (payload.get('checkpointAgent') or 'openclaw').strip() or 'openclaw'
            checkpoint_status = (payload.get('checkpointStatus') or 'on_track').strip() or 'on_track'
            blocked_on = payload.get('blockedOn') or []
            if isinstance(blocked_on, str):
                blocked_on = [x.strip() for x in blocked_on.split('\n') if x.strip()]
            path, data, current_lane = find_task(task_id)
            if not path:
                return self._send_json(404, {'ok': False, 'error': 'task_not_found'})
            if lane and lane not in ORDER:
                return self._send_json(400, {'ok': False, 'error': 'invalid_lane'})

            now = datetime.now().astimezone().isoformat()
            charter = data.setdefault('task_charter', {})
            checkpoints = data.setdefault('checkpoints', [])
            current_lane = current_lane or 'active'
            target_lane = lane or current_lane
            charter['updated_at'] = now
            charter['panel_content'] = content_draft
            charter['panel_content_title'] = content_title
            charter['panel_content_body'] = content_body
            charter['panel_content_tags'] = content_tags
            charter['panel_content_notes'] = content_notes
            charter['status'] = STATUS_BY_LANE[target_lane]
            if target_lane == 'awaiting_human':
                charter['owner'] = 'human'
            elif target_lane == 'awaiting_audit':
                charter['owner'] = 'claude_code'
            else:
                charter['owner'] = 'openclaw'

            checkpoint_id = f"panel-{now.replace(':','').replace('-','')}"
            checkpoints.append({
                'checkpoint_id': checkpoint_id,
                'timestamp': now,
                'agent': checkpoint_agent,
                'phase': STATUS_BY_LANE[target_lane],
                'status': checkpoint_status,
                'changes_or_findings': [summary] if summary else ['面板更新任务内容'],
                'evidence': [{
                    'kind': 'observed',
                    'statement': 'Task updated from task board panel.',
                    'source': 'web/task-board.html'
                }],
                'risks': [],
                'next_owner': charter['owner'] if target_lane != 'active' else 'human',
                'blocked_on': blocked_on,
                'acceptance_progress': [],
                'next_step': next_step,
                'needs_clarification': False,
                'changed_paths': [str(path)],
                'substantive_update': True,
                'alignment_decision': 'approve'
            })

            new_path = path
            if target_lane != current_lane:
                new_path = TASK_ROOT / target_lane / path.name
                new_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(new_path, data)
                if new_path != path and path.exists():
                    path.unlink()
            else:
                write_json(path, data)
            return self._send_json(200, {'ok': True, 'task': task_id, 'lane': target_lane})

        if parsed.path == '/api/create-task':
            task_id = (payload.get('taskId') or '').strip()
            goal = (payload.get('goal') or '').strip()
            lane = (payload.get('lane') or 'active').strip()
            if not task_id:
                return self._send_json(400, {'ok': False, 'error': 'missing_task_id'})
            if lane not in ORDER:
                return self._send_json(400, {'ok': False, 'error': 'invalid_lane'})
            existing, _, _ = find_task(task_id)
            if existing:
                return self._send_json(400, {'ok': False, 'error': 'task_exists'})
            now = datetime.now().astimezone().isoformat()
            filename = f"{task_id}.json"
            path = TASK_ROOT / lane / filename
            content_tags = payload.get('contentTags') or []
            if isinstance(content_tags, str):
                content_tags = [x.strip() for x in content_tags.split('\n') if x.strip()]
            data = {
                'version': '1.1',
                'task_charter': {
                    'task_id': task_id,
                    'repo_path': str(ROOT),
                    'goal': goal or 'New task created from task board panel.',
                    'non_goals': [],
                    'allowed_paths': ['.ai/tasks'],
                    'acceptance_checks': [],
                    'known_facts': [],
                    'open_questions': [],
                    'owner': 'human' if lane == 'awaiting_human' else ('claude_code' if lane == 'awaiting_audit' else 'openclaw'),
                    'status': STATUS_BY_LANE[lane],
                    'task_type': 'ops',
                    'priority': 'p2',
                    'decision_owner': 'human',
                    'timebox_minutes': 30,
                    'obsidian_note_path': '',
                    'obsidian_sync_mode': '',
                    'created_at': now,
                    'updated_at': now,
                    'panel_content': payload.get('contentDraft') or '',
                    'panel_content_title': payload.get('contentTitle') or '',
                    'panel_content_body': payload.get('contentBody') or '',
                    'panel_content_tags': content_tags,
                    'panel_content_notes': payload.get('contentNotes') or '',
                },
                'checkpoints': [{
                    'checkpoint_id': f"panel-create-{now.replace(':','').replace('-','')}",
                    'timestamp': now,
                    'agent': 'openclaw',
                    'phase': STATUS_BY_LANE[lane],
                    'status': STATUS_BY_LANE[lane] if lane in ('blocked', 'awaiting_human', 'awaiting_audit', 'done') else 'on_track',
                    'changes_or_findings': [goal or 'Task created from panel.'],
                    'evidence': [{
                        'kind': 'observed',
                        'statement': 'Task created from task board panel.',
                        'source': 'web/task-board.html'
                    }],
                    'risks': [],
                    'next_owner': 'human',
                    'blocked_on': [],
                    'acceptance_progress': [],
                    'next_step': 'Open the task card and continue editing details.',
                    'needs_clarification': False,
                    'changed_paths': [str(path)],
                    'substantive_update': True,
                    'alignment_decision': 'approve'
                }],
                'completion_audits': []
            }
            write_json(path, data)
            return self._send_json(200, {'ok': True, 'task': task_id, 'lane': lane})

        if parsed.path == '/api/task-action':
            action = (payload.get('action') or '').strip()
            task_id = (payload.get('taskId') or '').strip()
            path, data, lane = find_task(task_id)
            if not path:
                return self._send_json(404, {'ok': False, 'error': 'task_not_found'})
            now = datetime.now().astimezone().isoformat()
            if action == 'delete_demo':
                if 'demo' not in task_id:
                    return self._send_json(400, {'ok': False, 'error': 'only_demo_allowed'})
                path.unlink(missing_ok=True)
                return self._send_json(200, {'ok': True, 'action': action})
            if action == 'archive_done':
                target = TASK_ROOT / 'done' / path.name
                data.setdefault('task_charter', {})['status'] = 'done'
                data['task_charter']['owner'] = 'openclaw'
                data['task_charter']['updated_at'] = now
                data.setdefault('checkpoints', []).append({
                    'checkpoint_id': f"panel-archive-{now.replace(':','').replace('-','')}",
                    'timestamp': now,
                    'agent': 'openclaw',
                    'phase': 'done',
                    'status': 'done',
                    'changes_or_findings': ['Task archived to DONE from panel.'],
                    'evidence': [{'kind': 'observed', 'statement': 'Task archived from task board panel.', 'source': 'web/task-board.html'}],
                    'risks': [], 'next_owner': 'openclaw', 'blocked_on': [], 'acceptance_progress': [],
                    'next_step': 'No further action.', 'needs_clarification': False, 'changed_paths': [str(target)],
                    'substantive_update': True, 'alignment_decision': 'approve'
                })
                write_json(target, data)
                if target != path:
                    path.unlink(missing_ok=True)
                return self._send_json(200, {'ok': True, 'action': action})
            if action == 'clone_task':
                new_id = (payload.get('newTaskId') or '').strip()
                if not new_id:
                    return self._send_json(400, {'ok': False, 'error': 'missing_new_task_id'})
                existing, _, _ = find_task(new_id)
                if existing:
                    return self._send_json(400, {'ok': False, 'error': 'task_exists'})
                clone = json.loads(json.dumps(data))
                clone['task_charter']['task_id'] = new_id
                clone['task_charter']['status'] = 'executing'
                clone['task_charter']['owner'] = 'openclaw'
                clone['task_charter']['created_at'] = now
                clone['task_charter']['updated_at'] = now
                clone_path = TASK_ROOT / 'active' / f'{new_id}.json'
                clone.setdefault('checkpoints', []).append({
                    'checkpoint_id': f"panel-clone-{now.replace(':','').replace('-','')}",
                    'timestamp': now,
                    'agent': 'openclaw',
                    'phase': 'executing',
                    'status': 'on_track',
                    'changes_or_findings': [f'Cloned from {task_id}.'],
                    'evidence': [{'kind': 'observed', 'statement': 'Task cloned from panel.', 'source': 'web/task-board.html'}],
                    'risks': [], 'next_owner': 'human', 'blocked_on': [], 'acceptance_progress': [],
                    'next_step': 'Review cloned task and continue editing.', 'needs_clarification': False,
                    'changed_paths': [str(clone_path)], 'substantive_update': True, 'alignment_decision': 'approve'
                })
                write_json(clone_path, clone)
                return self._send_json(200, {'ok': True, 'action': action, 'task': new_id})
            if action == 'route_agent':
                agent = (payload.get('agent') or '').strip()
                if agent not in ('codex', 'claude_code', 'openclaw'):
                    return self._send_json(400, {'ok': False, 'error': 'invalid_agent'})
                # Just set owner — engine.py will dispatch on next cycle (≤60s)
                data.setdefault('task_charter', {})['updated_at'] = now
                data['task_charter']['owner'] = agent
                data.setdefault('checkpoints', []).append({
                    'checkpoint_id': f"panel-route-{now.replace(':','').replace('-','')}",
                    'timestamp': now,
                    'agent': 'openclaw',
                    'phase': data.get('task_charter', {}).get('status', 'executing'),
                    'status': 'on_track',
                    'changes_or_findings': [f'Routed task to {agent} from panel. Engine will dispatch.'],
                    'evidence': [{'kind': 'observed', 'statement': 'Agent route requested from task board panel.', 'source': 'web/task-board.html'}],
                    'risks': [], 'next_owner': agent, 'blocked_on': [], 'acceptance_progress': [],
                    'next_step': f'Engine will dispatch {agent} on next cycle.', 'needs_clarification': False,
                    'changed_paths': [str(path)], 'substantive_update': True, 'alignment_decision': 'approve'
                })
                write_json(path, data)
                return self._send_json(200, {'ok': True, 'action': action, 'agent': agent})
            if action == 'run_script':
                script = (payload.get('script') or '').strip()
                args = payload.get('args') or []
                if not script:
                    return self._send_json(400, {'ok': False, 'error': 'missing_script'})
                script_path = ROOT / 'scripts' / script
                if not script_path.exists():
                    return self._send_json(404, {'ok': False, 'error': f'script_not_found: {script}'})
                import subprocess
                cmd = [sys.executable, str(script_path)] + args
                proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                pid = proc.pid
                data.setdefault('task_charter', {})['updated_at'] = now
                data['task_charter']['owner'] = 'claude_code'
                rt = data['task_charter'].setdefault('runtime', {})
                rt['status'] = 'running'
                rt['pid'] = pid
                rt['started_at'] = now
                data.setdefault('checkpoints', []).append({
                    'checkpoint_id': f"panel-run-script-{now.replace(':','').replace('-','')}",
                    'timestamp': now, 'agent': 'claude_code', 'phase': 'executing', 'status': 'on_track',
                    'changes_or_findings': [f'Script {script} launched from panel (PID {pid}).'],
                    'evidence': [{'kind': 'observed', 'statement': f'Script launched: {" ".join(cmd)}', 'source': 'web/task-board.html'}],
                    'risks': [], 'next_owner': 'claude_code', 'blocked_on': [], 'acceptance_progress': [],
                    'next_step': f'Monitor script PID {pid}.', 'needs_clarification': False,
                    'changed_paths': [str(path)], 'substantive_update': True, 'alignment_decision': 'approve'
                })
                write_json(path, data)
                return self._send_json(200, {'ok': True, 'action': action, 'pid': pid, 'script': script})
            return self._send_json(400, {'ok': False, 'error': 'unknown_action'})

        if parsed.path == '/api/human-decision':
            task_id = payload.get('taskId', '').strip()
            action = payload.get('action', '').strip()
            note = payload.get('note', '').strip()
            if not task_id or not action:
                return self._send_json(400, {'ok': False, 'error': 'missing taskId or action'})
            # Find task file
            path = None
            for lane in ORDER:
                candidate = TASK_ROOT / lane / f'{task_id}.json'
                if candidate.exists():
                    path = candidate
                    break
            if not path:
                return self._send_json(404, {'ok': False, 'error': 'task_not_found'})
            data = json.loads(path.read_text())
            charter = data.get('task_charter', {})
            now_str = datetime.now().astimezone().isoformat()
            # Build checkpoint
            cp = {
                'checkpoint_id': f'human-decision-{now_str}',
                'timestamp': now_str,
                'agent': 'human',
                'phase': 'human_decision',
                'status': 'on_track',
                'changes_or_findings': [f'Human decision: {action}'],
                'evidence': [],
                'risks': [],
                'blocked_on': [],
                'acceptance_progress': [],
                'needs_clarification': False,
                'changed_paths': [],
                'substantive_update': True,
            }
            if note:
                cp['changes_or_findings'].append(f'Note: {note}')
            # Route based on action
            if action == 'approve':
                cp['next_owner'] = 'codex'
                cp['next_step'] = note or 'Human approved. Proceed to next phase.'
                cp['alignment_decision'] = 'approve'
                charter['owner'] = 'codex'
                charter['status'] = 'executing'
                target_lane = 'active'
            elif action == 'reject':
                cp['next_owner'] = 'codex'
                cp['next_step'] = note or 'Human rejected. Rework required.'
                cp['alignment_decision'] = 'reject'
                charter['owner'] = 'codex'
                charter['status'] = 'executing'
                target_lane = 'active'
            else:
                cp['next_owner'] = 'human'
                cp['next_step'] = note or action
                cp['alignment_decision'] = 'hold'
                target_lane = None  # stay
            data.setdefault('checkpoints', []).append(cp)
            charter['updated_at'] = now_str
            data['task_charter'] = charter
            write_json_locked(path, data)
            # Move lane if needed
            if target_lane and path.parent.name != target_lane:
                dest = TASK_ROOT / target_lane / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                path.rename(dest)
            return self._send_json(200, {'ok': True, 'action': action, 'targetLane': target_lane})

        return self._send_json(404, {'ok': False, 'error': 'not_found'})


def main():
    # Port: THREEBODY_PORT env var takes priority, then project.json, then default 8765
    port = int(os.environ.get('THREEBODY_PORT', '0')) or None
    name = 'Task Board'
    project_config = ROOT / 'project.json'
    if project_config.exists():
        try:
            cfg = json.loads(project_config.read_text())
            if not port:
                port = cfg.get('dashboard_port', 8765)
            name = cfg.get('project_name', 'Task Board')
        except Exception:
            pass
    if not port:
        port = 8765
    class ReusableServer(ThreadingHTTPServer):
        allow_reuse_address = True
        address_family = socket.AF_INET
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            super().server_bind()
    server = ReusableServer(('0.0.0.0', port), Handler)
    print(f'3body Dashboard v{API_VERSION} [{name}] at http://127.0.0.1:{port}/task-board.html')
    print(f'  ROOT: {ROOT}')
    print(f'  WEB:  {WEB}')
    server.serve_forever()


if __name__ == '__main__':
    main()
