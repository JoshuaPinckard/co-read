"""Eval set for the Grep-replacement benchmark, orchestrator implementation.

Independent of the Codex-authored extractor. Two implementations of the same
spec, cross-checked, per the discipline that caught a ninefold counting
disagreement on the hazard numbers.

Emits one record per Grep call that has a resolvable outcome. See
instruments/retrieval/SPEC.md for the schema and the label definitions.
"""
import json, os, sys, pathlib, collections
from datetime import datetime

ROOT = pathlib.Path(r"C:/Users/USER/.claude/projects")
OUT = pathlib.Path(r"C:/Users/USER/Desktop/Blast-Radius/exploratory/retrieval")
OUT.mkdir(parents=True, exist_ok=True)
WINDOWS = (60, 300, 900)

def ts_of(rec):
    t = rec.get("timestamp")
    if not t: return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def result_paths(tur):
    """Paths a Grep result surfaced. Grep returns either a filenames list or text."""
    if isinstance(tur, dict):
        for key in ("filenames", "files", "paths"):
            v = tur.get(key)
            if isinstance(v, list):
                return [str(x) for x in v if isinstance(x, (str, bytes))]
        v = tur.get("content") or tur.get("stdout")
        if isinstance(v, str):
            tur = v
    if isinstance(tur, str):
        out = []
        for line in tur.splitlines():
            line = line.strip()
            # files_with_matches gives bare paths; content mode gives path:line:text
            if not line or line.startswith(("Found ", "No matches", "--")):
                continue
            cand = line.split(":")[0] if ":" in line[2:] else line
            if ("/" in cand or "\\" in cand) and len(cand) < 400:
                out.append(cand)
        return out[:500]
    return []

def norm(p, cwd):
    if not p: return None
    p = str(p)
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    return os.path.normcase(os.path.normpath(p))

def scan_file(fp, agent_hint):
    """Return chronological events for one transcript: greps, reads, and results."""
    events = []
    pending = {}          # tool_use_id -> (kind, ts, input, cwd, agent)
    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            t = ts_of(rec)
            agent = rec.get("agentId") or agent_hint or rec.get("sessionId")
            cwd = rec.get("cwd")
            msg = rec.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and t is not None:
                        name, inp, tid = b.get("name"), b.get("input"), b.get("id")
                        if not isinstance(inp, dict):
                            continue
                        if name == "Grep":
                            pending[tid] = ("grep", t, inp, cwd, agent)
                            events.append(["grep", t, agent, tid, inp, cwd, []])
                        elif name in ("Read", "NotebookRead"):
                            p = norm(inp.get("file_path") or inp.get("notebook_path"), cwd)
                            if p: events.append(["read", t, agent, tid, None, cwd, p])
                    elif b.get("type") == "tool_result":
                        tid = b.get("tool_use_id")
                        if tid in pending:
                            tur = rec.get("toolUseResult")
                            if tur is None:
                                tur = b.get("content")
                            _, _, _, c, _ = pending.pop(tid)
                            for e in events:
                                if e[0] == "grep" and e[3] == tid:
                                    e[6] = [norm(x, c) for x in result_paths(tur)]
                                    break
            # result may also arrive as a bare toolUseResult record
            tur = rec.get("toolUseResult")
            if tur is not None and isinstance(msg, dict):
                pass
    events.sort(key=lambda e: e[1])
    return events

def main():
    files = list(ROOT.rglob("*.jsonl"))
    sys.stderr.write(f"{len(files)} transcripts\n")
    n_grep = n_kept = n_abandoned = n_noresult = 0
    win_counts = collections.Counter()
    out = open(OUT / "evalset-orchestrator.jsonl", "w", encoding="utf-8")
    for i, fp in enumerate(files):
        if i % 250 == 0:
            sys.stderr.write(f"  {i}/{len(files)}  kept={n_kept}\n"); sys.stderr.flush()
        hint = fp.name[:-6] if fp.name.startswith("agent-") else None
        try:
            events = scan_file(fp, hint)
        except Exception:
            continue
        greps = [e for e in events if e[0] == "grep"]
        reads = [(e[1], e[2], e[6]) for e in events if e[0] == "read"]
        grep_ts = [(e[1], e[2]) for e in greps]
        for g in greps:
            n_grep += 1
            _, gt, gag, gid, ginp, gcwd, gpaths = g
            if not gpaths:
                n_noresult += 1
            rec = {
                "id": f"{fp.stem}:{gid}",
                "ts": gt, "agent": gag, "cwd": gcwd,
                "query": {k: ginp.get(k) for k in
                          ("pattern", "path", "glob", "type", "output_mode", "-i", "head_limit")
                          if ginp.get(k) is not None},
                "returned_paths": gpaths,
            }
            resolvable = False
            for w in WINDOWS:
                fr = sorted({p for rt, ra, p in reads
                             if ra == gag and gt < rt <= gt + w and p})
                fg = any(og > gt and og <= gt + w and oa == gag for og, oa in grep_ts)
                rec[f"followed_by_read_{w}"] = fr
                rec[f"followed_by_grep_{w}"] = fg
                if fr or fg:
                    resolvable = True
                    win_counts[w] += 1
            if not resolvable:
                n_abandoned += 1
                continue
            n_kept += 1
            out.write(json.dumps(rec) + "\n")
    out.close()
    print(f"Grep calls seen        : {n_grep}")
    print(f"  with no result paths : {n_noresult}")
    print(f"  abandoned (excluded) : {n_abandoned}")
    print(f"  KEPT                 : {n_kept}   retention {n_kept/max(1,n_grep):.1%}")
    for w in WINDOWS:
        print(f"  resolvable within {w:>3}s : {win_counts[w]}")

if __name__ == "__main__":
    main()
