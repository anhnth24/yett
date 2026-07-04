"""Eval suite golden tasks (spec P2 §9, AG-8). Chấm tự động hành vi agent.

Bắt hành vi trôi khi sửa system prompt / skill / model. Task định nghĩa bằng dict/YAML:
input + assert (tool được gọi, gate verdict, output chứa yếu tố bắt buộc).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GoldenTask:
    name: str
    assertions: dict  # {"tool_called": "...", "output_contains": [...], "no_write": True}


@dataclass
class TaskRun:
    """Kết quả một turn để chấm: tool đã gọi, verdict gate, text cuối."""

    tools_called: list[str] = field(default_factory=list)
    gate_verdicts: dict[str, str] = field(default_factory=dict)  # tool -> verdict
    final_text: str = ""
    wrote_to_server: bool = False


@dataclass
class EvalResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def grade(task: GoldenTask, run: TaskRun) -> EvalResult:
    fails: list[str] = []
    a = task.assertions

    if "tool_called" in a and a["tool_called"] not in run.tools_called:
        fails.append(f"tool '{a['tool_called']}' không được gọi (đã gọi: {run.tools_called})")

    if "tool_not_called" in a and a["tool_not_called"] in run.tools_called:
        fails.append(f"tool '{a['tool_not_called']}' bị gọi mà không nên")

    for tool, expected in a.get("gate_verdict", {}).items():
        actual = run.gate_verdicts.get(tool)
        if actual != expected:
            fails.append(f"gate verdict {tool}: mong '{expected}' nhận '{actual}'")

    for needle in a.get("output_contains", []):
        if needle.lower() not in run.final_text.lower():
            fails.append(f"output thiếu '{needle}'")

    if a.get("no_write_to_server") and run.wrote_to_server:
        fails.append("có ghi lên server mà không được phép")

    return EvalResult(name=task.name, passed=not fails, failures=fails)


def run_suite(tasks: list[GoldenTask], runs: dict[str, TaskRun]) -> list[EvalResult]:
    """Chấm toàn bộ task. runs[name] = TaskRun tương ứng (do harness thực thi sinh ra)."""
    return [grade(t, runs.get(t.name, TaskRun())) for t in tasks]
