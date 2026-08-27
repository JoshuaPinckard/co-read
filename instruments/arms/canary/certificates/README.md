# Calibration certificates — 2026-08-25

These are retained outputs of six actual short calibration probes; version
queries are recorded separately and are not model probes.

- `CANARY-2026-08-25-20260826T022151Z-e983b0a8f209.json` is the immutable first
  joint run (four probes). Its overall verdict is `FAIL`: Claude independently
  certified marker-fired/marker-absent, while Codex's clean leg emitted no
  marker but refused the original hidden-context wording instead of returning
  the run-specific ACK.
- `CANARY-2026-08-25-20260826T023217Z-7a310cb09bbf.json` is the targeted Codex
  rerun (two probes) after the prompt was reduced to a benign precedence task.
  Its verdict is `PASS`.

Each JSON file has an immutable SHA-256 sidecar and hashed raw evidence under
`evidence/`. `canary.py check-set` independently revalidates the Claude surface
from the first source and Codex from the second, and counts both distinct source
runs for an aggregate of six probes against the eight-probe job ceiling.
