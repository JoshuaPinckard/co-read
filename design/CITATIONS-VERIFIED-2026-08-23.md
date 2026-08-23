# Verified citations — quarantine cleared

Ten citations from the prior-art sweep were quarantined as uncorroborated, five
of them flagged as likely fabricated. All ten were fetched through two
independent routes (the arXiv abstract page and the arXiv API's verbatim Atom
metadata), and the four load-bearing systems had their PDFs extracted and
searched locally rather than summarised.

**All ten exist. The fabrication flag was wrong on all five.**

## Methodological warning — a summarizer invented a quote

On the single most load-bearing detail, a web-fetch summarizer returned the
quote *"claims can be declared at file, function, line-range, or byte-range
granularity"* for Claim Plane. Local PDF extraction shows **"line-range" and
"byte-range" appear zero times in that paper.** The real text is: *"Resources can
denote files, bounded regions, symbols, concepts, contracts, routes, schemas,
configuration, or documents."*

No paper in this set uses byte ranges; "byte" is zero hits in CoAgent and ATM as
well. **Any single-pass summarizer quote in `PRIOR-ART-2026-08-23.md` is
unverified and must not be relied on.** That file stays quarantined; this one
supersedes it for these ten items.

## The systems

| Paper | Architecture | Claim granularity | Estimator |
|---|---|---|---|
| **Claim Plane** — Nikolaev, 24 Jul 2026, arXiv:2607.21909 | Shared logical resource space, deterministic pre-write admission, **fail-closed** | Symbol/concept-typed "bounded regions" — not offsets | No |
| **CoAgent** — Lyu, Dingyan Zhang, Wu, Wei, Chen (SJTU), 13 Jun 2026, arXiv:2606.15376 | Shared tree in place: *"writes take effect immediately: live state offers no private buffer or fork"*. **Advisory** — *"Control therefore turns advisory: the runtime informs, the agent repairs"* | **File / directory** — *"a file, a directory"* | No |
| **ATM** — Eagl Huang, 29 Jun 2026, arXiv:2607.00041 | Shared worktree, broker-mediated pre-write admission, **fail-closed** | Adapter-defined "atoms": *"a function, a class method, a registry entry, a JSON record, a numeric scalar, a text range"* | No |
| **CooperBench** — Khatua et al. (11 authors incl. Diyi Yang), 19 Jan 2026, arXiv:2601.13295 | Isolate-and-merge: separate Docker containers, `git merge-file` after | n/a — benchmark | No |
| **CAID** — Geng & Neubig, 23 Mar 2026, arXiv:2603.21489 | Isolate-and-merge, explicitly: *"isolated workspaces… branch-and-merge is a central coordination mechanism"* | n/a | No |
| **Co-Coder** — Yang, Nie, Chandra, Gannutin, Lin, Chaudhuri, 31 May 2026, arXiv:2606.00953 | Up-front partitioning, then isolate-and-merge | n/a | No — graph partitioning, not region risk |
| **grite** — Sarkar, 17 Jun 2026, arXiv:2606.19616 | Shared repo, TTL leases in `refs/grite/locks` | **Task/issue level**, not sub-file | No |
| **AgenticFlict** — Ogenrwot & Businge, 4 Apr 2026, arXiv:2604.03551 | Dataset: 142K PRs, 27.67% conflict rate | n/a | No |
| **AI Agent PRs on GitHub** — Xu, Subramanian/Subramonian, Karthik, 6 Jul 2026, arXiv:2607.04697 | Empirical study | n/a | No |
| **Caprese** — Tavakoli & Alimadadi, 19 Jun 2026, arXiv:2606.21187 | No agents — human JavaScript maintenance tooling | n/a | **Yes** |

Author corrections against the sweep: CoAgent's second author is **Dingyan**
Zhang, not "Dingdan", and is second not third. Co-Coder's "Chandra" is Ethan
Chandra, third author; senior author is Swarat Chaudhuri. Xu et al.'s second
author renders as "Subramanian" via the API and "Subramonian" on the abstract
page — unresolved, flagged rather than guessed.

## What this corrects

- **"Five works occupy the claim-plus-advisory-radius architecture" is wrong
  twice.** It is **three** — Claim Plane, CoAgent, ATM — and only **CoAgent** is
  advisory. The other two are explicitly fail-closed, the opposite posture.
- **Byte-range claims are genuinely unoccupied.** CoAgent is file/directory.
  ATM's atoms are adapter-defined. Claim Plane's regions are symbol-typed.

## Reads are already instrumented — but never as a training signal

Five systems the sweep missed entirely:

- **S-Bus** — Sajjad Khan, 16 May 2026, arXiv:2605.17076. Reconstructs each
  agent's read set from observed HTTP GET traffic without modifying agents.
  Cited by both CoAgent and ATM. **74% of reads on its SWE-bench workload are
  invisible to the HTTP surface**, and agent self-reports over-claim usage by
  32–49%.
- **STORM** — arXiv:2605.20563, May 2026. Shared codebase, explicitly rejects
  worktree isolation. *"it accumulates a read snapshot Si recording every file it
  has observed and the version at observation time."* File-granularity, exact
  version validation, no estimator. States the pain point directly: *"Two agents
  editing different functions in the same file trigger a false-positive
  rejection."*
- **Atomix** — arXiv:2602.14849. *"The runtime records reads and effects during
  execution"*, at tool/resource level.
- **Shepherd** — arXiv:2605.10913 (Stanford / Northeastern). Reversible execution
  traces; an LLM supervisor lifts CooperBench pair pass rate 28.8% → 54.7%.
- **ATCC** — arXiv:2603.13906. RL policy weighing blocking against abort cost —
  over SQL transactions, not code regions.

**Confirmed absence.** No work uses recorded agent reads as *dependency ground
truth* to train or validate a region-risk predictor. Reads are captured for exact
staleness validation, never as a training signal. Search terms returning nothing
on point are recorded in the agent transcript.

## The race, named

Claim Plane §10 is titled *"Research Agenda: From Conservative Planning to
Learned Semantic Dependency"*: *"The natural next step is not to place more
authority inside the planner, but to add a learned semantic-dependency model
(SDM) as a sensor between deterministic filtering and expensive frontier
supervision."* ATM: *"dynamic read reconstruction remains future work."*

Two groups have publicly named this estimator as the obvious next move as of July
2026. The field is empty but staked.

## Caprese's 22%

Caprese ranks candidate impacted entities under an inspection budget from runtime
plus co-change signals, validated against expert-curated sets. Its key reported
result: **only 22% overlap between history-based and dynamic signals.** Direct
methodological prior art for the scoring function, and the strongest external
evidence available that co-change and actual dependency are substantially
different objects.

## Claims that could NOT be verified

- Whether "Subramanian" or "Subramonian" is correct for arXiv:2607.04697.
- Caprese's 22% figure is taken from the paper's own reporting; not independently
  reproduced.
- S-Bus's 74% and 32–49% figures likewise.

## What would change this verdict

A paper published after 2026-08-23 that builds a region-risk estimator trained on
recorded agent reads. Given Claim Plane §10, that is a live possibility on a
months timescale, not years.
