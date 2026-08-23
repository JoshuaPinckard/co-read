"""Render the per-repository replay results and the §7A comparison."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from common import (
    CORPUS_PATH,
    OUTPUT_ROOT,
    REPOSITORIES,
    RESULT_ROOT,
    STREAM_ROOT,
    load_json,
    utc_now,
)


OUTPUT_PATH = OUTPUT_ROOT / "RESULTS.md"

MODEL_LABELS = {
    "cochange_time_decayed": "Co-change, time-decayed",
    "cochange_plain_confidence": "Co-change, plain confidence",
    "path_name_similarity": "Path/name similarity",
    "popularity_control": "Popularity — control",
    "random_draw": "Random draw — chance",
}

BASELINE = {
    "p_at_1": 0.500,
    "p_at_10": 0.204,
    "r_at_10": 0.411,
    "r_at_20": 0.488,
    "empty_radius_rate": 0.017,
}


def metric(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def percent(value: float | None) -> str:
    return "—" if value is None else f"{100.0 * value:.1f}%"


def microseconds(value: float | None) -> str:
    return "—" if value is None else f"{value:,.1f}"


def delta(value: float | None, comparator: float) -> str:
    return "—" if value is None else f"{value - comparator:+.3f}"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_records() -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    corpus = load_json(CORPUS_PATH, default={}) or {}
    results: dict[str, dict[str, Any]] = {}
    streams: dict[str, dict[str, Any]] = {}
    for repository in REPOSITORIES:
        slug = repository["slug"]
        results[slug] = load_json(RESULT_ROOT / f"{slug}.json", default={}) or {
            "status": "missing",
            "repository": repository,
            "failure": "result file is missing",
        }
        streams[slug] = load_json(STREAM_ROOT / f"{slug}.meta.json", default={}) or {
            "status": "missing",
            "repository": repository,
            "failure": "stream metadata is missing",
        }
    hashes = {
        result.get("implementation", {}).get("harness_sha256")
        for result in results.values()
        if result.get("status") == "ok"
    }
    if len(hashes) > 1:
        raise RuntimeError(f"refusing to mix successful results from different harnesses: {sorted(hashes)}")
    return corpus, results, streams


def direct_answer(results: dict[str, dict[str, Any]]) -> list[str]:
    successful = [result for result in results.values() if result.get("status") == "ok"]
    wins_both = 0
    above_js_p1 = 0
    above_js_r10 = 0
    for result in successful:
        models = result["models"]
        cochange = models["cochange_time_decayed"]
        popularity = models["popularity_control"]
        random_draw = models["random_draw"]
        wins_both += (
            cochange["r_at_10"] > popularity["r_at_10"]
            and cochange["r_at_10"] > random_draw["r_at_10"]
        )
        above_js_p1 += cochange["p_at_1"] >= BASELINE["p_at_1"]
        above_js_r10 += cochange["r_at_10"] >= BASELINE["r_at_10"]

    count = len(successful)
    if count and wins_both == count:
        verdict = (
            "**Co-change's predictive signal transfers, but §7A's numeric effect size and near-ceiling "
            "recommendation do not transfer uniformly.** Time-decayed co-change retains signal relative to "
            "both controls across all selected axes, yet the non-JavaScript results include sharp R@10 "
            "collapses. In the protocol's operational sense, §7A's numbers are a regime artifact; the "
            "co-change mechanism itself is not. Treating 0.500/0.411 as a language-independent ceiling would "
            "be wrong; formula design remains worth investigating in the weak regimes, although this pass "
            "does not show that a different formula would succeed. JavaScript itself cannot be isolated as "
            "the cause because language and repository regime are confounded."
        )
    elif count and wins_both >= (count + 1) // 2:
        verdict = (
            "**The selected axes provide evidence that co-change retains predictive signal beyond JavaScript, "
            "but the transfer is not uniform.** It beats both controls on R@10 in a majority, not all, of the "
            "successful repositories."
        )
    elif count:
        verdict = (
            "**The result is ambiguous.** Co-change does not beat both controls on R@10 in a majority of the "
            "successful selected axes, so this run does not establish cross-language transfer."
        )
    else:
        verdict = "**No verdict is available because no repository completed replay successfully.**"

    return [
        verdict,
        "",
        (
            f"Across {count} successful members of the **10 repositories selected to span named axes**, "
            f"time-decayed co-change beat both popularity and random on R@10 in {wins_both}/{count}, met or "
            f"exceeded §7A's P@1 0.500 in {above_js_p1}/{count}, and met or exceeded §7A's R@10 0.411 in "
            f"{above_js_r10}/{count}. It did not dominate the controls at every precision cutoff. These are "
            "descriptive counts, not a random-sample generalization."
        ),
    ]


def render() -> str:
    corpus, results, streams = load_records()
    lines: list[str] = [
        "# Cross-language co-change replay",
        "",
        f"Generated at `{utc_now()}`. Scope: **10 repositories selected to span named axes**; this is not a random sample of repositories.",
        "",
        "## Direct answer",
        "",
        *direct_answer(results),
        "",
        "The written protocol does not define a numerical width for “roughly the same band,” so this report does not convert that phrase into a post-hoc equivalence threshold.",
        "",
        "## Per-repository results",
        "",
        "P@K uses the fixed denominator K, with missing ranks counted as misses. Recall is averaged per seed query. Timings cover ranked-list production only and are implementation/machine diagnostics; factorized large cliques move exact work into query time, so co-change microseconds are not directly comparable with §7A's optimized 8–15.5 µs lookup. Shared co-change candidate-history expansion was timed once per seed and its full duration charged to each co-change model; the two reported medians estimate standalone calls and are not additive wall time for the combined run.",
        "",
        "| Repository | Model | P@1 | P@10 | R@10 | R@20 | Empty radius | Median query µs |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for repository in REPOSITORIES:
        result = results[repository["slug"]]
        if result.get("status") != "ok":
            lines.append(f"| {repository['name']} | **FAILED** | — | — | — | — | — | — |")
            continue
        for model_key, label in MODEL_LABELS.items():
            model = result["models"][model_key]
            lines.append(
                f"| {repository['name']} | {label} | {metric(model['p_at_1'])} | "
                f"{metric(model['p_at_10'])} | {metric(model['r_at_10'])} | "
                f"{metric(model['r_at_20'])} | {percent(model['empty_radius_rate'])} | "
                f"{microseconds(model['median_query_microseconds'])} |"
            )

    provider = results["hashicorp__terraform-provider-random"]
    if provider.get("status") == "ok":
        provider_random = provider["models"]["random_draw"]
        provider_share = provider["coverage"]["largest_query_commit_share"]
        lines.extend(
            [
                "",
                (
                    "Two interpretation cautions: time decay is not uniformly better than plain confidence, "
                    "and model dominance depends on the metric. In terraform-provider-random, for example, "
                    f"the random control has P@1 {provider_random['p_at_1']:.3f} but R@10 "
                    f"{provider_random['r_at_10']:.3f}; one broad commit contributes "
                    f"{percent(provider_share)} of all queries, so query-weighted rank-one precision can be "
                    "high while ten recommendations cover little of the large ground truth."
                ),
            ]
        )

    lines.extend(
        [
            "",
            "No confidence interval is computed across queries: seeds from the same commit are dependent. No interval is reported because this run did not perform the required whole-commit resampling.",
            "",
            "## Comparison with §7A",
            "",
            "The comparator is §7A's 907-commit Node result: time-decayed co-change P@1 0.500, P@10 0.204, R@10 0.411, R@20 0.488, and empty-radius rate 1.7%. The other two Node histories in §7A were reported as directional cold-start cases, not as an aggregate comparator.",
            "",
            "| Repository | P@1 | Δ vs 0.500 | R@10 | Δ vs 0.411 | Empty | Δ vs 1.7 pp |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for repository in REPOSITORIES:
        result = results[repository["slug"]]
        if result.get("status") != "ok":
            lines.append(f"| {repository['name']} | — | — | — | — | — | — |")
            continue
        model = result["models"]["cochange_time_decayed"]
        empty_delta_pp = 100.0 * (model["empty_radius_rate"] - BASELINE["empty_radius_rate"])
        lines.append(
            f"| {repository['name']} | {metric(model['p_at_1'])} | {delta(model['p_at_1'], 0.500)} | "
            f"{metric(model['r_at_10'])} | {delta(model['r_at_10'], 0.411)} | "
            f"{percent(model['empty_radius_rate'])} | {empty_delta_pp:+.1f} |"
        )

    commons = results["apache__commons-lang"]
    ripgrep = results["BurntSushi__ripgrep"]
    lines.extend(["", "## Test-convention effect", ""])
    if commons.get("status") == "ok" and ripgrep.get("status") == "ok":
        commons_path = commons["models"]["path_name_similarity"]
        ripgrep_path = ripgrep["models"]["path_name_similarity"]
        lines.extend(
            [
                (
                    "The expected pattern does **not** appear cleanly. Apache Commons Lang's mirrored tree has "
                    f"higher path P@1/P@10 ({commons_path['p_at_1']:.3f}/{commons_path['p_at_10']:.3f}) than "
                    f"ripgrep ({ripgrep_path['p_at_1']:.3f}/{ripgrep_path['p_at_10']:.3f}), but Apache has "
                    f"lower path R@10 ({commons_path['r_at_10']:.3f} vs {ripgrep_path['r_at_10']:.3f}). "
                    "Thus the precision side is consistent with the test-convention story while the recall side "
                    "runs against it. Under this literal prefix-first operationalization, path affinity is not a "
                    "clean measure of the convention everyone assumes."
                ),
                "",
                "**Confidence: low-to-moderate.** The contrast is directly measured at fixed SHAs, but §7A did not specify its exact path formula, and commit-size/path-layout differences confound the two repositories.",
            ]
        )
    else:
        lines.append("The effect could not be tested because one or both required repositories failed.")

    lines.extend(
        [
            "",
            "## Coverage, caps, exclusions, and failures",
            "",
            "| Repository | Reachable / first-parent commits | Replayed | Eligible commits | No-query commits (<2 claimable files) | Queries | Largest commit share | Added files excluded | Delete→re-add new IDs | Rename records | Cap/failure |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    corpus_records = corpus.get("repositories", {})
    for repository in REPOSITORIES:
        slug = repository["slug"]
        corpus_record = corpus_records.get(slug, {})
        result = results[slug]
        stream = streams[slug]
        commit_counts = f"{corpus_record.get('reachable_commit_count', '—'):,} / {corpus_record.get('first_parent_commit_count', '—'):,}" if corpus_record else "—"
        if result.get("status") == "ok":
            coverage = result["coverage"]
            cap_or_failure = stream.get("cap_reason") or "none"
            lines.append(
                f"| {repository['name']} | {commit_counts} | {coverage['commits_replayed']:,} | "
                f"{coverage['eligible_commit_count']:,} | "
                f"{coverage['commits_replayed'] - coverage['eligible_commit_count']:,} | "
                f"{coverage['query_count']:,} | "
                f"{percent(coverage['largest_query_commit_share'])} | "
                f"{coverage['created_files_excluded_from_ground_truth']:,} | "
                f"{coverage['delete_then_readd_new_identity_count']:,} | "
                f"{stream.get('rename_count', 0):,} | {cap_or_failure} |"
            )
        else:
            failure = result.get("failure") or stream.get("failure") or corpus_record.get("failure") or "unknown failure"
            lines.append(f"| {repository['name']} | {commit_counts} | — | — | — | — | — | — | — | — | **{failure}** |")

    clone_failures = [
        f"{repository['name']}: {corpus_records.get(repository['slug'], {}).get('failure', 'unknown failure')}"
        for repository in REPOSITORIES
        if corpus_records.get(repository["slug"], {}).get("status") != "ok"
    ]
    extraction_failures = [
        f"{repository['name']}: {streams[repository['slug']].get('failure', 'unknown failure')}"
        for repository in REPOSITORIES
        if streams[repository["slug"]].get("status") != "ok"
    ]
    replay_failures = [
        f"{repository['name']}: {results[repository['slug']].get('failure', 'unknown failure')}"
        for repository in REPOSITORIES
        if results[repository["slug"]].get("status") != "ok"
    ]
    lines.extend(
        [
            "",
            f"Clone failures: {'; '.join(clone_failures) if clone_failures else 'none.'} "
            f"Extraction failures: {'; '.join(extraction_failures) if extraction_failures else 'none.'} "
            f"Replay failures: {'; '.join(replay_failures) if replay_failures else 'none.'}",
            "",
            "Within each declared replay window, no commit was excluded or downweighted for size. A commit with k eligible files produced k queries. The largest-commit share above makes domination visible; per-commit aggregates remain in each result JSON for any later whole-commit bootstrap.",
            "",
            "Explicit protocol exclusions are fully exposed: commits outside the first-parent chain are not replayed; additions in the query commit are not claimable ground truth or seeds; commits with fewer than two remaining claimable files produce no queries; and each query seed is removed from its own candidate list. No path was filtered by extension, language, or directory. Deleted files remain claimable because they exist immediately before the commit. The two cap rows separately expose the omitted pre-window history.",
            "",
            "The cap trigger used all commits reachable from HEAD (>20,000); capped streams contain the latest 5,000 first-parent commits in chronological order. Their live-file universe is initialized from the parent tree at the window boundary, while learned indexes start empty. This is an explicit left truncation and can raise early-window silence.",
            "",
            "## Correctness guards",
            "",
            "- **Leakage:** every seed query asserts that the index is folded only through i−1; all seeds for a commit query one immutable pre-commit state, then the commit folds once.",
            "- **Created files:** A records are excluded from current ground truth and seeds, but folded for future history. The exact exclusion counts are in the coverage table.",
            "- **Renames:** extraction uses Git's explicit 50% similarity detector with unlimited rename candidates. Each emitted rename is one stable identity, queried under its old path then migrated; it never creates an old↔new pair. Git may lazily fetch blobs solely to classify renames; the five models do not inspect contents.",
            "- **Pass-1 content boundary — explicit deviation:** all five model features use only commit metadata and paths, but Git's 50% rename classifier hydrated blob objects during final extraction for six repositories (Hugo, Jupyter Notebook, Redis, Prometheus, Terraform, and Ansible). This exceeds a literal reading of the specification's ‘no file contents’ extraction boundary. The choice reduces modified-rename delete+add artifacts, but the heuristic can make both false-positive and false-negative identity assignments. Before/after object-store counts are preserved in each stream metadata file, and no blob bytes enter a model score.",
            "- **Merges:** history walks first-parent graph order and each merge is diffed once against parent 1.",
            "- **Random:** SHA-256-derived deterministic draws are uniform without replacement from files live immediately before i, excluding the seed.",
            "- **Popularity:** one global prior-touch ordering is built without seed features; the seed is only removed from the candidate list.",
            "- **Seeds/dependence:** every eligible file is a seed. Query counts, contributing commits, and largest-commit shares are reported; no query-level confidence interval is used.",
            "- **Trees and provenance:** every replayed final tree must equal `git ls-tree HEAD`; stream SHA-256, source HEAD, extraction flags, cap decision, and one common harness SHA-256 are verified before reporting.",
            "- **Large cliques:** small pair histories are materialized and large cliques factorized without exclusion. Integer counts combine before one division; decayed scores use the identical commit-index terms with `math.fsum`. Ten focused tests (including deterministic randomized boundary coverage) and the rerunnable full-stream `audit_representations.py` check verified ranking equivalence across representations.",
            "",
            "## Per-claim confidence",
            "",
            "- **The tabulated values describe these fixed SHAs under the reported operationalization — high confidence.** Every completed stream and tree passed structural checks, every prediction was asserted live at claim time, and the rerunnable full-stream ripgrep audit reproduced the hybrid/full co-change rankings after the rounding fix.",
            "- **Co-change retains predictive signal beyond JavaScript relative to both controls, but its effect size is regime-dependent — moderate confidence.** The claim is supported across deliberately different languages and layouts on R@10, but the 10 selected axes are not a probability sample, precision dominance is not uniform, results are heterogeneous, no whole-commit interval was computed, and sensitivity to exact-object-ID-only rename handling was not measured.",
            "- **§7A's exact numeric ceiling is not universal — moderate confidence.** Several direct per-repository deltas are large, but repository history shape, commit size, and left truncation are entangled with language.",
            "- **The expected Apache/ripgrep test-convention effect is mixed — low-to-moderate confidence.** Precision and recall point in different directions and the original path formula is unavailable.",
            "- **Timing comparisons with §7A are not established — high confidence in that limitation.** Hardware and the original timing boundary are unavailable, and exact factor expansion shifts work between update and query.",
            "",
            "## Claims that could NOT be verified",
            "",
            "- Bit-for-bit replication of §7A: its source code, repository SHAs, exact path formula, ties, RNG seed, candidate filtering, rename configuration, and short-list metric convention are unavailable.",
            "- Formal equivalence to §7A's “roughly same band”: the band has no numerical boundary and no equivalence margin was preregistered.",
            "- A language-causal effect: language, commit practice, age, tree layout, team behavior, and repository size are confounded in this selected corpus.",
            "- Complete ground-truth renames: Git's 50% similarity rule is heuristic and can make both false-positive and false-negative identity assignments; it cannot establish author intent for every move.",
            "- Sensitivity to a literal no-blob extraction: this run allowed Git's rename classifier to hydrate blob objects. A separate exact-object-ID-only rename replay would be required to quantify the effect of forbidding those reads.",
            "- Statistical uncertainty for model differences: no whole-commit bootstrap was run, and query-level intervals would be invalid.",
            "- Portable latency or direct equivalence to §7A's microsecond timings: hardware, implementation, caches, and work placement differ.",
            "- Whether delete→later-add should inherit old path history in §7A: this replay assigns a new identity and reports the count per repository.",
            "",
            "## What would change this verdict",
            "",
            "- Preregistered whole-commit bootstrap intervals for co-change's paired R@10 advantage over both controls that include zero across most selected non-JavaScript axes would change the transfer verdict to ambiguous or negative.",
            "- A preregistered equivalence margin followed by whole-commit intervals could either support or reject the stronger claim that §7A's 0.500/0.411 effect size transfers, rather than merely the mechanism.",
            "- Repeating the replay on a probability sample—or on additional independently chosen repositories within each language/layout axis—and seeing systematic collapse would overturn the current cross-language reading.",
            "- Re-running capped repositories with full warm history could show that their weak results are left-truncation artifacts; a large reversal would narrow any conclusion about scale regimes.",
            "- Recovering the original §7A harness and obtaining materially different results under its exact path/tie/identity conventions would invalidate direct formula-level comparisons here.",
            "- A no-blob sensitivity replay using exact-object-ID renames that materially changes the control gaps would narrow or reverse the reported transfer claim.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    atomic_write_text(OUTPUT_PATH, render())
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
