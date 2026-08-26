# ARMS SHIM

This standard-library-and-Git harness implements the six fixed arms in
`HYPOTHESES.md` section C, Amendment 2. The checked gate uses deterministic fake
subjects only; production adapters are present for Codex, Claude, and Gemini,
but `gate.py` never dispatches them.

The gate is fail-closed on same-day, integrity-checked Codex+Claude canary
evidence. A surface may come from a separate immutable certificate, with every
distinct source run counted once against the eight-probe ceiling. The gate then
runs the production scheduler through a selectable scripted-runner seam in two
independently created copies of the complete
2-site x 6-arm x 2-repeat matrix and compares canonical metrics plus the full
event stream after replacing timestamp coordinates and recomputing each event
hash chain.

Run unit tests without any model call:

```powershell
python -m unittest discover -s instruments\arms\shim\tests -v
```

Run the gate after calibration:

```powershell
python -m instruments.arms.shim.gate `
  --canary codex=instruments\arms\canary\certificates\CANARY-2026-08-25-20260826T023217Z-7a310cb09bbf.json `
  --canary claude=instruments\arms\canary\certificates\CANARY-2026-08-25-20260826T022151Z-e983b0a8f209.json `
  --output-root exploratory\arms\shim-gate-final
```

The default evidence root is `exploratory/arms/shim-gate/`; the report is
`exploratory/arms/SHIM-GATE.md`. Both are immutable by convention and the
runner refuses to overwrite either path.

Production limits are constants in `harness.py`: 20 minutes per agent, one
timeout retry before exclusion and slot redraw, 30-second write polling for arm
6, two optimistic loser retries, and escalation after three region side
switches. Gate-time fakes use scaled wall delays while metrics retain an
explicit deterministic logical schedule.

For reproducibility under host scheduling noise, shared-tree fake subjects both
launch and wait at a fake-only write barrier; the gate releases A, observes its
mechanical write-complete signal, then releases B. Every affected launch records
that control. The production unmediated arm has no barrier, and the report does
not generalize this one scripted interleaving to real scheduler behavior.
