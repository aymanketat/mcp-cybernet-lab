"""Summaries of runs: rates with 95% Wilson intervals, and paired comparison of two runs.

A paired comparison (same tasks, two topologies or two guards) uses the exact McNemar test on
the tasks where the two runs disagree.

`gateway_coverage` checks a guarded run against itself: every tool call the agent recorded must
also be in the gateway's log.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p-value: b and c are the counts of the two kinds of disagreement."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def load_results(run: Path) -> list[dict]:
    path = run / "results.jsonl" if run.is_dir() else run
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def rate(rows, key) -> str:
    k, n = sum(bool(r[key]) for r in rows), len(rows)
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.1%} [95% CI {lo:.1%}–{hi:.1%}]" if n else "0/0"


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    calls = sum(r["tool_calls"] for r in rows)
    tokens = sum((r.get("tokens") or {}).get("prompt_tokens", 0) + (r.get("tokens") or {}).get("completion_tokens", 0)
                 for r in rows)
    return {
        "task_runs": n,
        "leak_rate": rate(rows, "leaked"),
        "attempt_rate": rate(rows, "attempted"),
        "completion_rate": rate(rows, "completed"),
        "blocked_calls": sum(r["blocked"] for r in rows),
        "false_alarms": sum(r["false_alarms"] for r in rows),
        "tool_calls": calls,
        "errors": sum(bool(r["error"]) for r in rows),
        "tokens": tokens,
        "mean_seconds": round(sum(r.get("seconds", 0) for r in rows) / n, 1) if n else 0,
    }


def report(run: Path) -> str:
    rows = load_results(run)
    s = summarize(rows)
    lines = [f"Run {run.name}: {s['task_runs']} task runs"]
    for k in ("leak_rate", "attempt_rate", "completion_rate"):
        lines.append(f"  {k.replace('_', ' '):16s} {s[k]}")
    lines.append(f"  blocked calls    {s['blocked_calls']}   false alarms {s['false_alarms']}   "
                 f"tool calls {s['tool_calls']}   agent errors {s['errors']}")
    lines.append(f"  tokens           {s['tokens']}   mean seconds per task {s['mean_seconds']}")
    fam = defaultdict(list)
    for r in rows:
        fam[r.get("family") or "-"].append(r)
    if len(fam) > 1:
        lines.append("  by family:")
        for f, rs in sorted(fam.items()):
            lines.append(f"    {f:24s} leaks {sum(r['leaked'] for r in rs)}/{len(rs)}   "
                         f"blocked {sum(r['blocked'] for r in rs)}   false alarms {sum(r['false_alarms'] for r in rs)}")
    methods = defaultdict(int)
    for r in rows:
        for leak in r["leaks"]:
            for h in leak["canaries"]:
                methods[h["method"]] += 1
    if methods:
        lines.append("  leaked canaries by detection method: " +
                     ", ".join(f"{m} {c}" for m, c in sorted(methods.items())))
    if run.is_dir():
        (run / "summary.json").write_text(json.dumps(s, indent=1))
    return "\n".join(lines)


def compare(run_a: Path, run_b: Path, key: str = "leaked") -> str:
    a = {(r["task"], r.get("repeat", 0)): bool(r[key]) for r in load_results(run_a)}
    b = {(r["task"], r.get("repeat", 0)): bool(r[key]) for r in load_results(run_b)}
    pairs = sorted(a.keys() & b.keys())
    if not pairs:
        return "no common task runs to compare"
    only_a = sum(a[p] and not b[p] for p in pairs)
    only_b = sum(b[p] and not a[p] for p in pairs)
    both = sum(a[p] and b[p] for p in pairs)
    ka, kb = sum(a[p] for p in pairs), sum(b[p] for p in pairs)
    return "\n".join([
        f"Paired comparison on '{key}', {len(pairs)} common task runs",
        f"  {run_a.name}: {ka}/{len(pairs)}    {run_b.name}: {kb}/{len(pairs)}",
        f"  both {both}, only {run_a.name} {only_a}, only {run_b.name} {only_b}",
        f"  exact McNemar p = {mcnemar_exact(only_a, only_b):.4g}",
    ])


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def gateway_coverage(run: Path) -> tuple[bool, str]:
    """Does every tool call in the agent traces appear in the gateway log? (ok, report text)

    For each task run of a guarded run folder, the tool calls the agent recorded (trace.jsonl) are
    compared with those the gateway recorded (gateway.jsonl): same server, tool and arguments, in
    the same order, with the same block flag. A call the agent made and the gateway never saw
    would have reached a server around the gateway. Arguments are compared and never printed:
    they may hold canaries.
    """
    def canon(arguments) -> str:
        return json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)

    manifest = json.loads((run / "manifest.json").read_text()) if (run / "manifest.json").exists() else {}
    if (manifest.get("gateway") or {}).get("guard") == "none":
        return False, f"Run {run.name} was made without a gateway (gateway.guard none): nothing to compare"
    rows = load_results(run)
    in_trace = in_gateway = unknown = blocked_trace = blocked_gateway = 0
    decisions, starts, methods, problems = Counter(), Counter(), Counter(), []
    for r in rows:
        name = r.get("run") or f"{r['task']}-r{r.get('repeat', 0)}"
        trace, log = _jsonl(run / name / "trace.jsonl"), _jsonl(run / name / "gateway.jsonl")
        calls = [e for e in trace if e.get("type") == "tool_call"]
        unknown += sum(e.get("server") is None for e in calls)  # a tool no server offers: never sent
        sent = [(e["server"], e["tool"], canon(e.get("arguments")), bool(e.get("blocked")))
                for e in calls if e.get("server") is not None]
        seen = [(e["server"], e["tool"], canon(e.get("arguments")), e.get("decision") == "block")
                for e in log if e.get("type") == "tool_call"]
        for e in log:
            if e.get("type") == "start":
                starts[f"{e.get('guard')} v{e.get('policy_version')}"] += 1
            elif e.get("type") == "message":
                methods[e.get("method")] += 1
            elif e.get("type") == "tool_call":
                decisions[e.get("decision")] += 1
        elsewhere = sum(e.get("task") != name for e in log)
        in_trace += len(sent)
        in_gateway += len(seen)
        blocked_trace += sum(c[3] for c in sent)
        blocked_gateway += sum(c[3] for c in seen)
        if sent != seen or elsewhere or not log:
            only_trace = Counter(c[:3] for c in sent) - Counter(c[:3] for c in seen)
            only_gateway = Counter(c[:3] for c in seen) - Counter(c[:3] for c in sent)
            problems.append(
                f"  MISMATCH {name}: trace {len(sent)} calls, gateway log {len(seen)}"
                + ("; no gateway log" if not log else "")
                + f"; only in the trace {sorted(f'{s}.{t}' for s, t, _ in only_trace.elements())}"
                + f"; only in the gateway log {sorted(f'{s}.{t}' for s, t, _ in only_gateway.elements())}"
                + ("; same calls, but the order or a block flag differs"
                   if sent != seen and not only_trace and not only_gateway else "")
                + (f"; {elsewhere} gateway events carry another task's name" if elsewhere else ""))

    def counts(c: Counter) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: str(kv[0]))) or "none"

    lines = [f"Gateway coverage of run {run.name}: {len(rows)} task runs",
             f"  tool calls in agent traces {in_trace}   tool calls in gateway logs {in_gateway}",
             f"  task runs where the two differ (server, tool, arguments, order, block flag): {len(problems)}",
             f"  blocked calls: traces {blocked_trace}, gateway logs {blocked_gateway}   "
             f"gateway decisions: {counts(decisions)}",
             f"  guard and policy version at task start: {counts(starts)}",
             f"  other MCP messages in the gateway logs: {counts(methods)}"]
    if unknown:
        lines.append(f"  calls to a tool no server offers (answered by the agent itself, never sent): {unknown}")
    return not problems and bool(rows), "\n".join(lines + problems)
