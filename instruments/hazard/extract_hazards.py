"""Independent re-derivation of the read-then-foreign-write hazard rate.

Walks the Claude Code transcript corpus and counts events where agent A reads a
path and a DIFFERENT agent B then writes that same path within a window. That is
the precondition for the semantic invalidation the blast-radius formula exists to
predict -- observed directly rather than proxied by co-change.

This is deliberately a second, independent implementation. A subagent produced
these numbers first; load-bearing figures are re-derived from primary artifacts
rather than taken from an agent report.

Exposure, not invalidation. This counts when the precondition occurred. It does
NOT establish that the reading agent's work was actually wrong afterwards.
"""
import json, os, sys, gzip, collections, pathlib

ROOT = pathlib.Path(r"C:/Users/USER/.claude/projects")
OUT = pathlib.Path(r"C:/Users/USER/Desktop/Blast-Radius/exploratory/hazard")
OUT.mkdir(parents=True, exist_ok=True)

READ_TOOLS = {"Read", "NotebookRead"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Source-code extensions only. The subagent reported ~38% of hazards land on
# markdown coordination files (BUILD-QUEUE.md, MEMORY.md); those are real
# contention but they are not the code-coupling phenomenon under study.
CODE_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".go", ".rs",
            ".java", ".rb", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh",
            ".ps1", ".sql", ".tf", ".yaml", ".yml", ".json", ".toml"}

def iso_ms(ts):
    # 2026-08-23T10:19:31.123Z -> epoch seconds float
    if not ts: return None
    try:
        from datetime import datetime
        t = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return None

def walk_events():
    files = list(ROOT.rglob("*.jsonl"))
    sys.stderr.write(f"{len(files)} transcript files\n")
    for i, fp in enumerate(files):
        if i % 250 == 0:
            sys.stderr.write(f"  {i}/{len(files)}\n"); sys.stderr.flush()
        agent_hint = None
        name = fp.name
        if name.startswith("agent-"):
            agent_hint = name[:-6]           # agent-<id>
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line[0] != "{":
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    ts = iso_ms(rec.get("timestamp"))
                    if ts is None:
                        continue
                    agent = rec.get("agentId") or agent_hint or rec.get("sessionId")
                    if not agent:
                        continue
                    for blk in content:
                        if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                            continue
                        tool = blk.get("name")
                        inp = blk.get("input")
                        if not isinstance(inp, dict):
                            continue
                        path = inp.get("file_path") or inp.get("notebook_path")
                        if not path:
                            continue
                        if tool in READ_TOOLS:
                            kind = "r"
                        elif tool in WRITE_TOOLS:
                            kind = "w"
                        else:
                            continue
                        yield (ts, str(agent), kind, os.path.normcase(str(path)))
        except Exception:
            continue

def main():
    by_path = collections.defaultdict(list)
    total = 0
    for ts, agent, kind, path in walk_events():
        total += 1
        by_path[path].append((ts, agent, kind))

    sys.stderr.write(f"{total} path-bearing read/write events over {len(by_path)} paths\n")

    windows = {"60s": 60, "5m": 300, "1h": 3600, "24h": 86400}
    for label, code_only in (("all-files", False), ("code-only", True)):
        hz = {k: 0 for k in windows}
        hz_paths = {k: set() for k in windows}
        readers = {k: set() for k in windows}
        writers = {k: set() for k in windows}
        multi_agent_paths = 0
        considered = 0
        for path, evs in by_path.items():
            if code_only and os.path.splitext(path)[1].lower() not in CODE_EXT:
                continue
            considered += 1
            agents = {a for _, a, _ in evs}
            if len(agents) > 1:
                multi_agent_paths += 1
            evs.sort()
            reads = [(t, a) for t, a, k in evs if k == "r"]
            writes = [(t, a) for t, a, k in evs if k == "w"]
            if not reads or not writes:
                continue
            for rt, ra in reads:
                for wt, wa in writes:
                    if wt <= rt or wa == ra:
                        continue
                    d = wt - rt
                    for lab, span in windows.items():
                        if d <= span:
                            hz[lab] += 1
                            hz_paths[lab].add(path)
                            readers[lab].add(ra)
                            writers[lab].add(wa)
        print(f"\n=== {label} ===")
        print(f"paths considered: {considered}   touched by >1 agent: {multi_agent_paths}"
              f" ({multi_agent_paths/considered:.1%})" if considered else "none")
        for lab in ("60s", "5m", "1h", "24h"):
            print(f"  within {lab:>4}: {hz[lab]:>7} hazard events on {len(hz_paths[lab]):>5} paths"
                  f"   readers={len(readers[lab]):>5} writers={len(writers[lab]):>5}")

if __name__ == "__main__":
    main()
