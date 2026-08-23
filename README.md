# Blast-Radius

Designing and validating a scoring function that predicts, given a claimed
region of a repository, which other regions a change to it may semantically
invalidate — for **arbitrary unseen repositories, in any language, with no
parser**.

Principal investigator: Josh Pinckard. Method paper, research-first: the result
goes into the product as a verified addition, not before.

The problem statement is `brief/BRIEF-v1-2026-08-23.md`, frozen and hashed. It is
authored elsewhere and was being edited live while early design work ran against
it; the frozen copy is the instrument, and no model is pointed at the live file.
See `brief/FREEZE.md` for what that cost.

## Status

Design phase. Nothing has been measured by this project yet. Everything in
`design/` is elicited design input, not evidence, and is tagged with the brief
version it was produced against.

`GATES.md` is the ladder. Gate 0 is discharged except for the missing remote;
Gate 1 is open, and the open defects are listed there.

## Layout

| Path | Holds |
|---|---|
| `brief/` | The frozen problem statement and its freeze record |
| `design/` | Elicited design input — prior art, generalization spec, adversarial reviews |
| `instruments/` | Prompts sent to models, and the scripts that hash and verify them |
| `corpus/` | Repository sets. Exploratory and confirmatory are separated and the confirmatory set is touched once |
| `generator/` | Synthetic repository-and-history generation |
| `scorer/` | The formula |
| `analysis/` | Evaluation and confirmatory analysis |
| `exploratory/` `confirmatory/` | Run outputs, separated so exploratory tooling cannot produce confirmatory data |
| `docs/` | Methods sources and the decision log |

## Standing rules

**No magic numbers.** Every constant in the scorer carries exactly one of two
tags: `@derived(<one-line derivation>)` or `@perrepo(<index-time statistic it
reads>)`. There is no third category. A constant justifiable only by "it worked
on our repository" has no legal tag and cannot ship.

**The fixture rule.** A measurement on the authors' own repositories may refute a
claim written down as universal beforehand, or confirm that the machinery
produces the quantity the derivation predicts. It may not set a constant, choose
between two designs, or appear in shipped code. Killing four of five proposals by
fixture measurement is ranking five proposals on one repository, whatever it is
called.

**Citations are quarantined until verified.** The prior-art sweep in `design/`
carries a list of citations that could not be corroborated. They are listed so
they can be checked, not so they can be cited.

**Claims carry per-claim confidence and the reason for it.** Not a
document-level label.

## Provenance of the method

The research conventions here are derived from LEAN-Bench, read and copied under
owner permission. Nothing in those trees was modified. The gate ladder is
**derived, not inherited** — the source ladder is per-run-type and cost-ordered,
and copying it verbatim would be a category error.
