PASS - ARMS SHIM scripted gate (20/20 checks passed); no real-subject draw launched

# ARMS SHIM gate

This gate executed 48 scripted draws: two independently created runs of two real Click sites, all six approved arms, and two repeats per arm. It also executed the contradictory Pygments dispatch screen once per run. No real subject was launched by the shim; the only model calls represented here are the 6 separately bounded canary calibration probes.
Every matrix cell and contradictory dispatch screen traversed `ProductionScheduler`; the injected runner was the deterministic fake policy, not a production CLI launcher.

The byte-intersecting site is Click `65eceb08e392e74dcc761be2090e951274ccbe36` (corpus `overlap`, strict byte intersection). The second site is Click `11abf2bff0f48b7f7b04b38b6a70fb102ef17662`, retained under its exact corpus label `boundary_only` as the Amendment 2 sensitivity class; it is not relabeled `same_file_disjoint` or `permissive`. The contract-screen site is Pygments `00a31bcae2f61ce74ccfabd05be2731bfc7a5a28` (`MUTUALLY_UNSATISFIABLE`). Base commit identities and their resolved tree hashes are both present in every event.

## Per-check evidence

### PASS: approved six-arm Amendment 2 is committed

Expected:

```json
{
  "approved_six_arm_text_present_at_head": true
}
```

Actual output:

```json
{
  "approved_six_arm_text_present_at_head": true,
  "head_commit": "2d5decf4c7bec95b4421527a7775e1ff019c1386",
  "head_hypotheses_sha256": "bdb9aa16adcdf6847d22713a18a39f9ba97735ee534ea029dacaf0fcd406ecbe",
  "pass": true,
  "real_draw_note": "worktree differs from committed approved blob; resolve before real draw",
  "worktree_hypotheses_sha256": "71bad695d4e1156f124c40b334bd798501ab1f508db12c4f7c16ae16b3e83c6c",
  "worktree_matches_head": false
}
```

### PASS: same-day canary prerequisite

Expected:

```json
{
  "pass": true,
  "required_surfaces": [
    "claude",
    "codex"
  ]
}
```

Actual output:

```json
{
  "aggregate_model_calls": 6,
  "calibration_days": [
    {
      "date": "2026-08-25",
      "utc_offset": "-07:00"
    }
  ],
  "errors": [],
  "maximum_model_calls": 8,
  "pass": true,
  "required_surfaces": [
    "claude",
    "codex"
  ],
  "surface_results": {
    "claude": {
      "actual_model_calls": 4,
      "calibration_day": {
        "basis": "calibration-host local calendar day with fixed UTC offset",
        "date": "2026-08-25",
        "utc_offset": "-07:00"
      },
      "certified_surfaces": [
        "claude"
      ],
      "errors": [],
      "pass": true,
      "path": "C:\\Users\\joshp\\Desktop\\Blast-Radius\\instruments\\arms\\canary\\certificates\\CANARY-2026-08-25-20260826T022151Z-e983b0a8f209.json",
      "required_surfaces": [
        "claude"
      ],
      "selected_surfaces": [
        "codex",
        "claude"
      ]
    },
    "codex": {
      "actual_model_calls": 2,
      "calibration_day": {
        "basis": "calibration-host local calendar day with fixed UTC offset",
        "date": "2026-08-25",
        "utc_offset": "-07:00"
      },
      "certified_surfaces": [
        "codex"
      ],
      "errors": [],
      "pass": true,
      "path": "C:\\Users\\joshp\\Desktop\\Blast-Radius\\instruments\\arms\\canary\\certificates\\CANARY-2026-08-25-20260826T023217Z-7a310cb09bbf.json",
      "required_surfaces": [
        "codex"
      ],
      "selected_surfaces": [
        "codex"
      ]
    }
  }
}
```

### PASS: frozen prompt manifest and shim-build prompt hash

Expected:

```json
{
  "all_manifest_rows_match": true,
  "shim_prompt_present": true
}
```

Actual output:

```json
{
  "checked_count": 28,
  "errors": [],
  "manifest": "prompts/HASHES.txt",
  "manifest_sha256": "d118c9d6efdf2ce42c3e6f443a19082d15117b3df83429e6dc1a5b6465567009",
  "pass": true,
  "shim_prompt": {
    "actual_bytes": 6362,
    "actual_sha256": "ab320cacf2f90cbcf5f12feaa541e6a4f8383aa8df637b3e2a652a4f88b30164",
    "expected_bytes": 6362,
    "expected_sha256": "ab320cacf2f90cbcf5f12feaa541e6a4f8383aa8df637b3e2a652a4f88b30164",
    "path": "prompts/job-shim-build.txt"
  }
}
```

### PASS: two sites x six arms x two repeats in each independent run

Expected:

```json
{
  "draws_per_run": 24,
  "unique_draws_per_run": 24
}
```

Actual output:

```json
{
  "run_1_draws": 24,
  "run_1_extra_cells": [],
  "run_1_missing_cells": [],
  "run_1_unique": 24,
  "run_2_draws": 24,
  "run_2_extra_cells": [],
  "run_2_missing_cells": [],
  "run_2_unique": 24
}
```

### PASS: all matrix cells exercised the production scheduler seam

Expected:

```json
{
  "matrix_rows": 48,
  "scheduler.kind": "production-runner-seam",
  "scheduler.scripted_gate_policy": true
}
```

Actual output:

```json
{
  "matrix_rows": 48,
  "nonconforming_draw_ids": []
}
```

### PASS: integration-attempted metrics retain the buildability oracle

Expected:

```json
{
  "all_have_oracle_and_buildable": true,
  "integration_attempted_min": 1
}
```

Actual output:

```json
{
  "integration_attempted_rows": 44,
  "missing_buildability_draw_ids": []
}
```

### PASS: event logs identical after timestamp normalization

Expected:

```json
"byte-identical"
```

Actual output:

```json
{
  "identical": true,
  "run_1_bytes": 10636514,
  "run_1_sha256": "48223d6ccf6d4f6f77787c8a08dbfb8fdf09701a58ab26e02bd03786c6a1f117",
  "run_2_bytes": 10636514,
  "run_2_sha256": "48223d6ccf6d4f6f77787c8a08dbfb8fdf09701a58ab26e02bd03786c6a1f117"
}
```

### PASS: metrics identical

Expected:

```json
"canonical-JSON-identical"
```

Actual output:

```json
{
  "identical": true,
  "run_1_sha256": "40b9ca9f55a2b14d4ab0b192417d0f145fda4983f466830047a7a973759ff715",
  "run_2_sha256": "40b9ca9f55a2b14d4ab0b192417d0f145fda4983f466830047a7a973759ff715"
}
```

### PASS: test-modifying cheater rejected by byte-identity rule

Expected:

```json
{
  "cheater_correct": false,
  "test_mismatch_count_min": 1
}
```

Actual output:

```json
{
  "attempts": [
    {
      "correct": false,
      "excluded": false,
      "finished": true,
      "instance_id": "pallets__click-65eceb08-a1-r2-A-s0-t0-ir0",
      "integration_retry": 0,
      "logical_seconds": 0.06,
      "mode": "cheater",
      "side": "A",
      "slot_redraw_index": 0,
      "timeout_retry": 0
    }
  ],
  "caught_complete_events": 1,
  "mismatch_paths": [
    "tests/test_formatting.py"
  ]
}
```

### PASS: staller timeout retry, exclusion, and slot redraw

Expected:

```json
{
  "fresh_redraws": 1,
  "staller_exclusions": 2,
  "summary_ids_match_attempts": true
}
```

Actual output:

```json
{
  "redrawn_attempts": [
    {
      "correct": true,
      "excluded": false,
      "finished": true,
      "instance_id": "pallets__click-11abf2bf-a1-r2-B-s1-t0-ir0",
      "integration_retry": 0,
      "logical_seconds": 0.1,
      "mode": "answer",
      "side": "B",
      "slot_redraw_index": 1,
      "timeout_retry": 0
    }
  ],
  "slot_redraw_instances": [
    "pallets__click-11abf2bf-a1-r2-B-s1-t0-ir0"
  ],
  "staller_attempts": [
    {
      "correct": false,
      "excluded": true,
      "finished": false,
      "instance_id": "pallets__click-11abf2bf-a1-r2-B-s0-t0-ir0",
      "integration_retry": 0,
      "logical_seconds": 1.0,
      "mode": "staller",
      "side": "B",
      "slot_redraw_index": 0,
      "timeout_retry": 0
    },
    {
      "correct": false,
      "excluded": true,
      "finished": false,
      "instance_id": "pallets__click-11abf2bf-a1-r2-B-s0-t1-ir0",
      "integration_retry": 0,
      "logical_seconds": 1.0,
      "mode": "staller",
      "side": "B",
      "slot_redraw_index": 0,
      "timeout_retry": 1
    }
  ],
  "timeout_excluded_instances": [
    "pallets__click-11abf2bf-a1-r2-B-s0-t0-ir0",
    "pallets__click-11abf2bf-a1-r2-B-s0-t1-ir0"
  ]
}
```

### PASS: arm 6 N=3 alternating-region escalation

Expected:

```json
{
  "side_sequence": [
    "A",
    "B",
    "A",
    "B"
  ],
  "side_switches": 3
}
```

Actual output:

```json
{
  "escalation": {
    "budget": 3,
    "region_key": "CHANGES.rst:anchor:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855:0:5813f499a289e454d252676daa565a32323541b131322ad0757e0c810164afec:64",
    "side_sequence": [
      "A",
      "B",
      "A",
      "B"
    ],
    "side_switches": 3
  },
  "escalation_count": 1
}
```

### PASS: arm 3 fixed-loser retry accounting

Expected:

```json
{
  "discarded_diff_seconds": 0.15,
  "loser": "B (later finisher)",
  "retry_compute_seconds": 0.11,
  "retry_count": 2,
  "wasted_compute_seconds_union": 0.21
}
```

Actual output:

```json
{
  "discarded_diff_seconds": 0.15000000000000002,
  "discarded_instance_ids": [
    "pallets__click-65eceb08-a3-r1-initial-paircycle0-B-s0-t0-ir0",
    "pallets__click-65eceb08-a3-r1-B-integration-retry-1-B-s0-t0-ir1"
  ],
  "integration_correct": true,
  "later_finisher": "B",
  "later_finisher_instance": "pallets__click-65eceb08-a3-r1-initial-paircycle0-B-s0-t0-ir0",
  "retry_compute_seconds": 0.11,
  "retry_instance_ids": [
    "pallets__click-65eceb08-a3-r1-B-integration-retry-1-B-s0-t0-ir1",
    "pallets__click-65eceb08-a3-r1-B-integration-retry-2-B-s0-t0-ir2"
  ],
  "wasted_compute_seconds": 0.21000000000000002
}
```

### PASS: arm 6 contradictory-task contract screen precedes dispatch

Expected:

```json
{
  "contradiction_surfaced": true,
  "scheduler.kind": "production-runner-seam",
  "subject_launches": 0
}
```

Actual output:

```json
{
  "contradiction_surfaced": true,
  "draw_id": "pygments__pygments-00a31bca-a6-r1",
  "launch_events": 0,
  "scheduler": {
    "arm2_start_barrier": false,
    "event_clock": "injected",
    "full_retry_safety_cap": 12,
    "kind": "production-runner-seam",
    "max_optimistic_retries": 2,
    "max_timeout_retries": 1,
    "region_alternation_budget": 3,
    "scripted_gate_policy": true,
    "wall_accounting": "injected"
  },
  "subject_launches": 0
}
```

### PASS: arm 6 contested writes attributed from mechanical logs only

Expected:

```json
{
  "all_nonempty_rates": 1.0,
  "contested_region_pairs_min": 1
}
```

Actual output:

```json
{
  "bad_rate_draws": [],
  "contested_region_pairs": 5
}
```

### PASS: append-only JSONL schema and hash chains

Expected:

```json
{
  "all_logs_valid": true,
  "log_count": 50
}
```

Actual output:

```json
{
  "event_count": 850,
  "failures": [],
  "log_count": 50
}
```

### PASS: no real subject in shim draws

Expected:

```json
{
  "launch_subject_cli": "scripted-fake"
}
```

Actual output:

```json
{
  "launch_events_by_run": {
    "run-1": 74,
    "run-2": 74
  },
  "unexpected_subject_clis": []
}
```

### PASS: fake-only shared-tree interleaving control is explicit

Expected:

```json
{
  "all_log_scripted_write_release_control": true,
  "arm_2_launches": 16,
  "release_order": [
    "A",
    "B"
  ]
}
```

Actual output:

```json
{
  "arm_2_launches": 16,
  "arm_2_launches_by_run": {
    "run-1": 8,
    "run-2": 8
  },
  "controlled_launches": 16,
  "production_barrier": false,
  "release_order": [
    "A",
    "B"
  ]
}
```

### PASS: coordinator remains dispatch-only with zero integration retries

Expected:

```json
{
  "integration_retries": 0
}
```

Actual output:

```json
{
  "nonzero_retry_draws": []
}
```

### PASS: file-lock under-declaration recorded but not blocked

Expected:

```json
{
  "counterfactual_refusal_seconds_positive": true,
  "violations_min": 1
}
```

Actual output:

```json
{
  "counterfactual_refusal_seconds": 0.16,
  "declaration_violations": 17,
  "schedule": "parallel-shared"
}
```

### PASS: protected exact roots and unused-mirror metadata unchanged

Expected:

```json
"before/after Git-state; exact fixture, prompt, arms, and used-mirror content; HYPOTHESES bytes; and unused-mirror metadata fingerprints identical"
```

Actual output:

```json
{
  "after": {
    "all_corpus_mirrors_metadata_manifest": {
      "byte_count": 3244838438,
      "content_hashed": false,
      "file_count": 4430,
      "manifest_sha256": "e3631731322388c1dbd914c65071c620f44db74ce7b9b656790629108d887c86",
      "root": "corpus/_conflict_mirrors"
    },
    "exact_content_manifests": {
      "corpus/_conflict_mirrors/pallets__click": {
        "byte_count": 10901300,
        "content_hashed": true,
        "file_count": 357,
        "manifest_sha256": "d38ffc633acb9525586d8f58e08f75068a681661b0ba71346df0886897bd4a98",
        "root": "corpus/_conflict_mirrors/pallets__click"
      },
      "corpus/_conflict_mirrors/pygments__pygments": {
        "byte_count": 29179224,
        "content_hashed": true,
        "file_count": 56,
        "manifest_sha256": "e7959041e083ad70cf3cf9647703d005fe537fb95d59e43eafb32c140e32de79",
        "root": "corpus/_conflict_mirrors/pygments__pygments"
      },
      "exploratory/arms": {
        "byte_count": 655559615,
        "content_hashed": true,
        "file_count": 59651,
        "manifest_sha256": "cde358b51af470ad663c422f1f5cf73a8997ac17340958a294407c4bee621cbf",
        "root": "exploratory/arms"
      },
      "fixture": {
        "byte_count": 53938058,
        "content_hashed": true,
        "file_count": 4018,
        "manifest_sha256": "dd19e325e69b81a1e641cfbf723a0188686ac21d161b3525213f30aaaf8e8c9b",
        "root": "fixture"
      },
      "instruments/arms": {
        "byte_count": 1727666,
        "content_hashed": true,
        "file_count": 129,
        "manifest_sha256": "b39fa2e8a34eccad846f6d9e9a6c5514a27df3c88646e985f7048e2e6758101d",
        "root": "instruments/arms"
      },
      "prompts": {
        "byte_count": 110999,
        "content_hashed": true,
        "file_count": 30,
        "manifest_sha256": "17e72d5560b26cd897a62dd1714bdef348ae6c7e92e22b99b6b595a94c287150",
        "root": "prompts"
      }
    },
    "hypotheses_worktree": {
      "path": "HYPOTHESES.md",
      "sha256": "71bad695d4e1156f124c40b334bd798501ab1f508db12c4f7c16ae16b3e83c6c",
      "size_bytes": 20159
    },
    "returncode": 0,
    "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "stdout_bytes": 273854,
    "stdout_sha256": "15740d9377695364634aeb52283a833a637e133a5af48659cf8f1bb8d811acac"
  },
  "before": {
    "all_corpus_mirrors_metadata_manifest": {
      "byte_count": 3244838438,
      "content_hashed": false,
      "file_count": 4430,
      "manifest_sha256": "e3631731322388c1dbd914c65071c620f44db74ce7b9b656790629108d887c86",
      "root": "corpus/_conflict_mirrors"
    },
    "exact_content_manifests": {
      "corpus/_conflict_mirrors/pallets__click": {
        "byte_count": 10901300,
        "content_hashed": true,
        "file_count": 357,
        "manifest_sha256": "d38ffc633acb9525586d8f58e08f75068a681661b0ba71346df0886897bd4a98",
        "root": "corpus/_conflict_mirrors/pallets__click"
      },
      "corpus/_conflict_mirrors/pygments__pygments": {
        "byte_count": 29179224,
        "content_hashed": true,
        "file_count": 56,
        "manifest_sha256": "e7959041e083ad70cf3cf9647703d005fe537fb95d59e43eafb32c140e32de79",
        "root": "corpus/_conflict_mirrors/pygments__pygments"
      },
      "exploratory/arms": {
        "byte_count": 655559615,
        "content_hashed": true,
        "file_count": 59651,
        "manifest_sha256": "cde358b51af470ad663c422f1f5cf73a8997ac17340958a294407c4bee621cbf",
        "root": "exploratory/arms"
      },
      "fixture": {
        "byte_count": 53938058,
        "content_hashed": true,
        "file_count": 4018,
        "manifest_sha256": "dd19e325e69b81a1e641cfbf723a0188686ac21d161b3525213f30aaaf8e8c9b",
        "root": "fixture"
      },
      "instruments/arms": {
        "byte_count": 1727666,
        "content_hashed": true,
        "file_count": 129,
        "manifest_sha256": "b39fa2e8a34eccad846f6d9e9a6c5514a27df3c88646e985f7048e2e6758101d",
        "root": "instruments/arms"
      },
      "prompts": {
        "byte_count": 110999,
        "content_hashed": true,
        "file_count": 30,
        "manifest_sha256": "17e72d5560b26cd897a62dd1714bdef348ae6c7e92e22b99b6b595a94c287150",
        "root": "prompts"
      }
    },
    "hypotheses_worktree": {
      "path": "HYPOTHESES.md",
      "sha256": "71bad695d4e1156f124c40b334bd798501ab1f508db12c4f7c16ae16b3e83c6c",
      "size_bytes": 20159
    },
    "returncode": 0,
    "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "stdout_bytes": 273854,
    "stdout_sha256": "15740d9377695364634aeb52283a833a637e133a5af48659cf8f1bb8d811acac"
  }
}
```

## Fake-subject script hashes

| Script | SHA-256 |
| --- | --- |
| `instruments/arms/shim/fakes/_driver.py` | `598a4abc99f530604090e2619c4ede86750388cc2feffd39377a359de91679b6` |
| `instruments/arms/shim/fakes/alternator.py` | `364a740b1a629c85de133f5aafdb89e13221c56c857fb9f2f173c4dc6f079246` |
| `instruments/arms/shim/fakes/answer.py` | `b9822eb22928248990fd0aaf27398a086f5f34077b1c99f21ec76dcab35302d1` |
| `instruments/arms/shim/fakes/benign.py` | `57faae50712d612a9403841617662bea5e2ff9b8ddeeb7c029bc104edda7c186` |
| `instruments/arms/shim/fakes/cheater.py` | `25820f9f53262ce0528dedfde7c2452fc1d194567578a4ba119a5bdfba313848` |
| `instruments/arms/shim/fakes/collision.py` | `65429dfadc6642fbd1477182363e520fff6feb14a9b22232833ebad925ca58b2` |
| `instruments/arms/shim/fakes/redraw.py` | `74bd564876d63aa8dc29e01e8b0b993ef6ed929bcaf447d6a574800837ef50d5` |
| `instruments/arms/shim/fakes/staller.py` | `4459b3e3a75545b9331f5dd651fefab7d308dda4cc561caf70d3e472184d6424` |

## Instrument source manifest

```json
{
  "file_count": 35,
  "manifest_sha256": "65f324283d2315a09a8ad97deb43ee3fb610e5ff85b7b7675e0849876f24d042",
  "per_file_hashes": "environment.json#instrument_source_sha256"
}
```

## CLI versions detected

| CLI | Present | Exact version output | Requested canary model |
| --- | --- | --- | --- |
| codex | true | `codex-cli 0.146.0` | `gpt-5.6-terra` |
| claude | true | `2.1.186 (Claude Code)` | `claude-sonnet-4-6` |
| gemini | true | `0.53.0` | `not probed` |
| git | true | `git version 2.46.0.windows.1` | `not probed` |
| python | true | `Python 3.11.9` | `not probed` |

## Canary certificates

```json
{
  "aggregate_check": {
    "aggregate_model_calls": 6,
    "calibration_days": [
      {
        "date": "2026-08-25",
        "utc_offset": "-07:00"
      }
    ],
    "errors": [],
    "maximum_model_calls": 8,
    "pass": true,
    "required_surfaces": [
      "claude",
      "codex"
    ],
    "surface_results": {
      "claude": {
        "actual_model_calls": 4,
        "calibration_day": {
          "basis": "calibration-host local calendar day with fixed UTC offset",
          "date": "2026-08-25",
          "utc_offset": "-07:00"
        },
        "certified_surfaces": [
          "claude"
        ],
        "errors": [],
        "pass": true,
        "path": "C:\\Users\\joshp\\Desktop\\Blast-Radius\\instruments\\arms\\canary\\certificates\\CANARY-2026-08-25-20260826T022151Z-e983b0a8f209.json",
        "required_surfaces": [
          "claude"
        ],
        "selected_surfaces": [
          "codex",
          "claude"
        ]
      },
      "codex": {
        "actual_model_calls": 2,
        "calibration_day": {
          "basis": "calibration-host local calendar day with fixed UTC offset",
          "date": "2026-08-25",
          "utc_offset": "-07:00"
        },
        "certified_surfaces": [
          "codex"
        ],
        "errors": [],
        "pass": true,
        "path": "C:\\Users\\joshp\\Desktop\\Blast-Radius\\instruments\\arms\\canary\\certificates\\CANARY-2026-08-25-20260826T023217Z-7a310cb09bbf.json",
        "required_surfaces": [
          "codex"
        ],
        "selected_surfaces": [
          "codex"
        ]
      }
    }
  },
  "requested_models": {
    "claude": "claude-sonnet-4-6",
    "codex": "gpt-5.6-terra"
  },
  "sources": {
    "claude": {
      "calibration_day": {
        "basis": "calibration-host local calendar day with fixed UTC offset",
        "date": "2026-08-25",
        "utc_offset": "-07:00"
      },
      "certified_surfaces": [
        "claude"
      ],
      "path": "instruments/arms/canary/certificates/CANARY-2026-08-25-20260826T022151Z-e983b0a8f209.json",
      "probe_budget": {
        "actual_model_calls": 4,
        "maximum_model_calls": 8,
        "planned_model_calls": 4,
        "version_queries_not_model_calls": 2
      },
      "verdict": "FAIL"
    },
    "codex": {
      "calibration_day": {
        "basis": "calibration-host local calendar day with fixed UTC offset",
        "date": "2026-08-25",
        "utc_offset": "-07:00"
      },
      "certified_surfaces": [
        "codex"
      ],
      "path": "instruments/arms/canary/certificates/CANARY-2026-08-25-20260826T023217Z-7a310cb09bbf.json",
      "probe_budget": {
        "actual_model_calls": 2,
        "maximum_model_calls": 8,
        "planned_model_calls": 2,
        "version_queries_not_model_calls": 1
      },
      "verdict": "PASS"
    }
  }
}
```

## Claims that could NOT be verified

- The supplied phrase **19 validated sites** is not supported by one uniform validation predicate. Actual inventory: `{"go_runner_eligible": 2, "go_validated_true": 0, "go_validation_scope": "Runner eligibility only: focal mapping, reproducible dependency availability, perturbation round-trip, and five-run base determinism. This is not the separate Phase 0 source/test red-green discrimination result.", "java_runner_passed": 6, "java_validated_true": 0, "numeric_total_python_plus_go_eligible_plus_java_passed": 19, "python_two_sided_validated": 11}`. The artifacts numerically total 19 only by adding 11 independently two-sided Python red/green sites, 2 Go runner-eligible sites explicitly marked `validated:false`, and 6 Java runner `passed` sites with no `validated:true` field. This shim therefore gates only two of the 11 sites accepted by the strict Python loader.
- The corrected Amendment 2 `permissive` population has no prepared, independently validated site artifact in this checkout. The second matrix site is explicitly a `boundary_only` sensitivity site, not evidence about the permissive stratum.
- The frozen `prompts/HASHES.txt` check covers the existing prompt artifacts (including `job-shim-build.txt`), but the actual subject task/declaration composition currently lives as code templates in `adapters.py` and is not a PI-frozen prompt artifact. Before any real draw, those exact templates and composition rules must be frozen under `prompts/` by a separately authorized change; this job was forbidden from modifying that directory.
- At gate time the working copy of `HYPOTHESES.md` differed from the committed approved blob at `HEAD`; a real-subject draw would remain forbidden by precondition 1 until the intended amendment state is committed.
- `otherwise buildable` is evaluated here only by the disclosed `python-source-syntax-compile-v1` screen over all non-test Python files. No preregistered repository full-build or full-suite command exists, so stronger buildability remains unverified.
- Real-agent efficacy, live concurrency timing, provider throttling, and model-specific behavior were not tested; this job was explicitly restricted to scripted fake subjects.
- Scripted per-wall throughput uses a deterministic schedule-aware critical path over retained fake durations (serial sums, parallel maxima, plus retries/declarations). It verifies metric plumbing and retry accounting, not host-clock or real-provider efficiency.
- The Gemini adapter and environment-manifest surface were implemented, and the installed CLI version was detected, but Gemini was deliberately not calibrated or called in this job. Production launch remains fail-closed for that uncalibrated surface.
- A requested model string in a canary certificate cannot prove a mutable provider-side alias snapshot unless the provider exposes an immutable resolved identifier.
- The six-call total is six subject-CLI model-probe invocations. Vendor-internal HTTP or inference retries inside one CLI invocation are opaque and could not be counted independently.
- Instruction discovery is version-sensitive. The canary covers the documented local, project, managed-policy, settings, rules, skills, plugin, MCP, and auto-memory candidates enumerated by the current instrument; undocumented future/server-managed channels remain outside the proof.
- Arm 2 and parallel arm 4 intentionally provide only shared-pair attribution because both subjects inhabit one tree. The 100% log-only contested-write attribution claim is gated for arm 6, where separate worktrees make principal attribution identifiable.
- Parallel arm 4 can score only declaration coverage against the shared pair-union snapshot. It cannot prove per-agent declaration accuracy: for example, two agents swapping undeclared paths can be hidden by the declared union. Per-side declaration accuracy is reported only where worktrees are attributable.
- Reproducibility of scripted shared-tree cells is certified for one disclosed fake-only A-then-B write-release interleaving after both processes launch. It does not estimate the distribution of operating-system schedules or outcomes for real unmediated arm 2; the production launcher has no such barrier.
- A real shared-tree completion snapshot is a non-atomic per-file filesystem walk. Without the fake-only completion handshake used by this gate, a peer can write between files in that walk, so the snapshot is mechanically retained but cannot certify one cross-file instant. This limitation does not affect arm 6, which uses separate worktrees.
- Filesystem snapshots observe retained differences at completion and at the configured poll instants. A write that is fully restored between snapshots is not observable. The 100% log-only attribution claim therefore applies to contested snapshot-visible retained regions, not to every filesystem write syscall.
- Protected-state evidence hashes exact bytes for `fixture/`, `prompts/`, `instruments/arms/`, `exploratory/arms/`, `HYPOTHESES.md`, and the two used Click/Pygments mirrors. Other unused corpus mirrors are covered only by path/kind/size/mode/mtime metadata, not by byte hashes.

## What would change this verdict

- Any failed check above, any hash-chain break, a non-scripted launch in the shim logs, or a mismatch between normalized runs changes the verdict to FAIL.
- Missing, stale, tampered, semantically invalid, or collectively over-budget Codex/Claude canary evidence prevents the first draw and changes the verdict to FAIL. A surface certified inside an otherwise failed immutable multi-surface run remains admissible only when its raw evidence independently revalidates.
- A preregistered uniform buildability oracle would replace the current syntax-only screen; a red oracle result would make affected integration outcomes incorrect.
- Go/Java sites may join the validated population only after artifacts demonstrate the same two-sided source/test red-green discrimination predicate used by the Python loader.
- Re-running with different source, fixture, site-manifest, corpus-line, patch, CLI, or fake-script hashes is a new instrument version and requires a fresh gate.
- A real draw remains blocked until the exact subject task and declaration prompts are PI-frozen and hash-manifested; changing them afterward requires a new instrument gate.

## Per-claim confidence

| Claim | Confidence | Reason |
| --- | --- | --- |
| Timestamp-normalized event and metric reproducibility | High | Compared complete canonical bytes from two independently cloned scratch repositories. |
| Cheater, timeout/redraw, arm-3 retry, and arm-6 escalation behavior | High | Each is asserted from append-only mechanical events plus the matching deterministic metric record. |
| P4 log-only attribution for contested arm-6 retained regions | High for this scripted gate | Snapshot-visible claims and byte regions come from filesystem state; subject prose is not parsed. Transient write-and-restore activity and generalization to real subjects were not tested. |
| Clean-room instruction-channel firing and absence | High for the certified CLI versions, paths, host, and timestamp | Both planted markers fired and both clean acknowledgements were observed under separate redirected rooms; retained responses and manifests are hash-checked. |
| Uniform 19-site validation | Low / contradicted by artifacts | Only 11 records satisfy the loader's independent two-sided `validated:true` and `VALIDATED` predicate. |
| Otherwise buildable | Moderate for Python syntax; not estimable for a full repository build | The gate runs the disclosed syntax-build screen, but no frozen full-build/full-suite oracle exists. |

## Evidence roots

- `exploratory/arms/shim-gate-final/run-1`
- `exploratory/arms/shim-gate-final/run-2`
- `exploratory/arms/shim-gate-final/gate-checks.json`
- `exploratory/arms/shim-gate-final/environment.json`

Interrupted development evidence (excluded from every verdict/check):

- `exploratory/arms/shim-gate` - retained after a pre-final run was stopped when the production-runner wiring audit found a scope blocker.
