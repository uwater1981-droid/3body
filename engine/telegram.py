"""
Lightweight Telegram notifier for 3body patrol.
Sends human decision requests via OpenClaw's main agent (kala_mac) Telegram channel.
"""
import subprocess

CHAT_ID = "8014043380"


def send_message(text: str) -> bool:
    """Send a text message via openclaw message send (main/kala_mac Telegram).
    Returns True on success."""
    try:
        result = subprocess.run(
            [
                'openclaw', 'message', 'send',
                '--channel', 'telegram',
                '--target', CHAT_ID,
                '-m', text,
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def format_human_decision_request(task_id: str, goal: str, next_step: str,
                                   findings: list[str], risks: list[str],
                                   blocked_on: list[str]) -> str:
    """Format a human decision request as a Telegram message."""
    lines = [
        "🔔 3body 需要你的决策",
        "",
        f"任务: {task_id}",
        f"目标: {goal}",
    ]
    if findings:
        lines.append("当前进展:")
        for f in findings[:3]:
            lines.append(f"  • {f[:100]}")
    if next_step:
        lines.append(f"需要决策: {next_step}")
    if blocked_on:
        lines.append("阻塞项:")
        for b in blocked_on:
            lines.append(f"  • {b[:100]}")
    if risks:
        lines.append("风险:")
        for r in risks[:2]:
            lines.append(f"  • {r[:100]}")
    lines.append("")
    lines.append("⏰ 30 分钟内未回复将自动降级为 OpenClaw 自决策")
    return "\n".join(lines)
