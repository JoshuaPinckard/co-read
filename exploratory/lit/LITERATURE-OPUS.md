# LITERATURE-OPUS: independent citation verification

Verifier: independent pass. Every fact below carries the URL it was confirmed at.
Nothing here is taken from memory. Items that could not be reached on the live web
are listed under "Claims that could NOT be verified" with the search terms used.

Method note: several publisher PDFs could not be read by the fetch tool (binary
stream). Those were downloaded and extracted locally with `pdftotext -layout`,
then read directly. Where a two-column table was scrambled by extraction, the
reconstruction was verified by checking that every row sums to its stated total;
those checks are shown inline so a reader can audit them.

---

## 1. Merge-conflict rate reconciliation

### 1.1 The rate table

Our study: 416 conflicted of 25,073 historical two-parent merges = 1.66%;
2.34% conditioned on both sides having nonempty diffs.

| Study | Population | Unit | Exact denominator | Reported rate |
|---|---|---|---|---|
| **Zimmermann 2007** (MSR) | 4 CVS projects: GCC, JBOSS, JEDIT, PYTHON | Per-file CVS workspace-update events | `C/(G+C)` = conflicting integrations / all integrations | **22.75% – 46.62%** |
| **Zimmermann 2007**, same paper | same | Commits | Commits (M and A) that led to a conflict, / all commits | **1.86% – 7.82%** |
| **Zimmermann 2007**, same paper | same | Per-file update events | `(G+C)/(W+U+P+G+C)` integration rate | **0.15% – 0.54%** |
| **Brun et al. 2011** (ESEC/FSE) | 9 OSS systems, 3.4M LOC | Historical merges | 3,562 historical merges | **564 = 16%** textual |
| **Brun et al. 2011**, same paper | same | *Speculative* pairwise merges | 292,921 potential merges | **55,498 = 19%** textual |
| **Brun et al. 2011**, same paper | Git, Perl5, Voldemort only | Historical merges | 1,428 textually-clean merges | **133 = 9.3%** build-or-test failure (see 1.4: this is the "33%" claim, corrected) |
| **Kasi & Sarma 2013** (ICSE) | 4 GitHub projects: Perl, Storm, Jenkins, Voldemort | Replayed shadow-repo integrations | #Merges = 185 / 88 / 505 / 380 | **merge conflicts 7.6% – 19.3%**; total conflicts (merge+build+test) 40% / 44% / 54% / 34% |
| **Ghiotto et al. 2018** (TSE) | 2,731 Java projects | Historical merges replayed | *No conflict rate is reported.* Implied: 25,328 failed / 960,366 replayed | **~2.6% implied** (see 1.3 - the paper itself cites 10–20% from others) |
| **Accioly et al. 2018** (EMSE) | 123 GitHub Java projects | Replayed historical merge scenarios | 70,047 merge scenarios | **4,141 = 5.91%** (semistructured/FSTMerge, Java files) |
| **Leßenich et al. 2018** (ASE jrnl) | 163 GitHub Java projects | Historical two-parent merge commits | 21,488 merge scenarios | **2,361 = 10.99%** any conflict; **1,379 = 6.41%** Java-code conflict |
| **Owhadi-Kareshk et al. 2019** (ESEM) | 744 GitHub repos, 7 languages | Historical two-parent merge commits | 267,657 merge scenarios | **21,734 = 8.12%** |
| **Ogenrwot & Businge 2026** (AgenticFlict) | 59K+ repos, AI-agent PRs | Simulated PR merges | 107,026 successfully simulated PRs | **29,609 = 27.67%** |

### 1.2 Which of our denominators each population resembles, and the unit question

The task hypothesised that the historical-merge vs replayed-scenario distinction
explains the gap. **It does not, and the real explanation is different.** Sorted
by how closely each matches our denominator:

**Closest match to "all two-parent merges" (our 1.66% denominator):**

- **Owhadi-Kareshk et al. 2019** is a near-exact methodological twin. Verbatim:
  *"MERGANSER, thus, identifies target merge scenarios by looking for commits with
  two parents. It then replays all identified 3-way merge scenarios by checking
  out Parent #1 and then using the git merge command to merge Parent #2's
  changes."* No common-file filter, no nonempty-diff filter. Rate: **8.12%**.
- **Leßenich et al. 2018** likewise: *"identified merge scenarios by filtering
  commits with multiple parent commits"* and re-ran each. Rate: **6.41%** (Java
  code) or **10.99%** (any conflict).

These two are historical merge commits, replayed with `git merge` - exactly our
unit and exactly our denominator. They cluster at **6–11%**, not high teens.

**Resembles "both-sides-common-file" (our most restrictive condition), or tighter:**

- **Zimmermann 2007's 23–47%** is `C/(G+C)`: conflicting integrations divided by
  integrations *only*. An "integration" is already a per-file event in which CVS
  *had* to merge because the developer had local changes to a file that had
  changed in the repository. This is conditioned on same-file concurrent
  modification having already happened, and it is per-file, not per-merge. It is
  tighter than our "both-sides-common-file" condition. Zimmermann's own
  commit-level number in the same table is **1.86%–7.82%**, which brackets our
  1.66%–2.34%.
- **Kasi & Sarma 2013's** denominator is #Merges from recursively integrating each
  developer's local commits into a shadow master - synthesised integration
  scenarios, not historical merge commits, and the merge counts are tiny relative
  to changesets (Perl: 185 merges against 23,079 changesets).

**Replayed / speculative integration scenarios (not historical merges):**

- **Brun et al. 2011's 19%** figure is explicitly speculative: 292,921 *potential*
  merges, i.e. every commit at which two developers who eventually merged could
  have merged earlier. Their historical figure is the separate 16% over 3,562
  merges.
- **AgenticFlict 2026's 27.67%** is over simulated PR merges for AI-agent PRs - a
  different population entirely (agent-authored PRs), and a PR denominator.

**Verdict on the unit hypothesis:** the historical-vs-replayed distinction explains
the *Brun 16% vs 19%* spread (small) and part of the Kasi/Zimmermann inflation, but
it does **not** explain our gap. The two studies that use our exact unit and our
exact denominator report 6.41%–10.99% and 8.12%. Our 1.66% is roughly 4–6x below
them. See section 5 for what this means for the draft.

### 1.3 Ghiotto et al. - full record, and what they actually measured

**Citation.** Ghiotto, G., Murta, L., Barros, M., & van der Hoek, A. (2020). On the
Nature of Merge Conflicts: A Study of 2,731 Open Source Java Projects Hosted by
GitHub. *IEEE Transactions on Software Engineering*, 46(8), 892–915.
DOI: 10.1109/TSE.2018.2871083.
Confirmed at: https://leomurta.github.io/papers/ghiotto2018.pdf (author copy,
carries the DOI on page 1) and
https://2019.icse-conferences.org/details/icse-2019-Journal-First-Paper/45/

> **Author correction for the draft.** The task brief called this "Ghiotto/Accioly
> et al." These are two different author lines. The TSE paper is
> **Ghiotto, Murta, Barros, van der Hoek**. Accioly is first author of a separate
> EMSE 2018 paper with Borba and Cavalcanti (section 1.5). Do not merge them.

**Population and denominator.** Verbatim from the paper: *"We then cloned the
repositories of these projects and replayed the merges (960,366 of them)"*, from
13,576 active Java projects. *"This led to 2,731 projects with 25,328 failed merges
and 175,805 conflicting chunks."*

**Critical: Ghiotto et al. do not report a conflict rate at all.** Their dataset is
conflicting merges *only* - non-conflicting merges were discarded (*"When the merge
did not lead to a conflict, it was discarded"*). The "10% to 20%" figure that this
paper is routinely cited for is not theirs; it is a citation to references [22] and
[23]. Verbatim from their introduction: *"Still, it has been reported that 10% to
20% of all merges fail [22], [23], with some projects experiencing rates of almost
50% [22], [24]."* Reference [22] is Brun et al. ESEC/FSE 2011, [23] is Kasi & Sarma
ICSE 2013, [24] is Zimmermann MSR 2007.

The implied rate from their own pipeline is 25,328 / 960,366 = **2.64%**. Treat
this as a lower bound only: the numerator is counted *after* discarding fork
projects and projects whose conflicts fell outside Java files, while the 960,366
denominator is measured before that filtering. The true rate over those 960,366
replayed merges is somewhere above 2.64% and is not stated. The paper's own threats
section concedes *"the number of merges we analyzed, thus, is a lower bound"*.

**What they measured about conflict anatomy** (this is what the novelty claim must
survive):

1. Number of conflicting chunks per failed merge. 40% of failed merges have a
   single chunk; 90% have ten or fewer; maximum 10,315 chunks.
2. **Size of each of the two versions of each conflicting chunk, in lines of code.**
   Verbatim: *"94% of the conflicting chunks have up to 50 LOC in each version
   (165,616 out of 175,805), 68% have up to ten LOC in each, and slightly over half
   (50%) five or fewer."* Also: *"Across all 175,805 conflicting chunks, 4,147 (2%)
   involve more than 50 LOC in both versions, while 6,042 (3%) have more than 50
   LOC in one version and less than 50 LOC in the other."* Per-project median and
   mean chunk sizes are tabulated.
3. Outermost Java language constructs present in each chunk.
4. Developer resolution strategy per chunk (version 1 / version 2 / concatenation /
   combination / new code / none).
5. Dependencies among chunks within the same failed merge.

**They do not measure bytes.** A case-insensitive search for "byte" across the full
extracted text of the paper returns **zero** occurrences. Their size unit is LOC
throughout, and their unit of spatial analysis is the *chunk* - the region git
already delimited with conflict markers. They never intersect the two sides' edit
ranges; the chunk boundary is taken as given from git's output. The same zero-hit
result holds for Accioly et al. 2018, Brun et al. 2011, and Kasi & Sarma 2013.

### 1.4 Brun et al. 2011 - and a propagated arithmetic error worth flagging

**Citation.** Brun, Y., Holmes, R., Ernst, M. D., & Notkin, D. (2011). Proactive
detection of collaboration conflicts. *ESEC/FSE '11: Proceedings of the 19th ACM
SIGSOFT Symposium and the 13th European Conference on Foundations of Software
Engineering*, 168–178. DOI: 10.1145/2025113.2025139.
Confirmed at: https://dl.acm.org/doi/10.1145/2025113.2025139 and full text at
https://www.cs.ubc.ca/~rtholmes/papers/fse_2011_brun.pdf

**Textual conflict rate.** Figure 4 ("Historical merges"), nine systems, **3,562
merges, 564 textual conflicts = 16%**. Reconstructed per-system (the two-column
extraction interleaves Figures 4 and 5; every row below sums exactly to its stated
merge count, and the total sums to 3,562, which is the audit):

| System | Merges | Textual conflicts |
|---|---|---|
| Git | 1,362 | 227 (17%) |
| Perl5 | 185 | 14 (8%) |
| Voldemort | 147 | 25 (17%) |
| Gallery3 | 458 | 42 (9%) |
| Insoshi | 93 | 23 (25%) |
| jQuery | 15 | 1 (7%) |
| MaNGOS | 192 | 81 (42%) |
| Rails | 362 | 51 (14%) |
| Samba | 748 | 100 (13%) |
| **total** | **3,562** | **564 (16%)** |

The paper's prose says both "17%" (*"one in six, or 17%, had textual conflicts"*)
and "16%" (conclusion: *"16% of all merges required human effort to resolve textual
conflicts"*). The 16% is the pooled figure over all 3,562; 17% is the
three-system subset. Cite 16% over 3,562 historical merges.

**Speculative rate.** Figure 5 ("Potential early merges"): **292,921 potential
merges, 55,498 = 19%** textual. Verbatim: *"On average, 19% of the potential merges
would have resulted in a textual conflict."* (Audit: the nine row values sum to
292,921 and the textual counts sum to 55,498.)

**Clean-merge build/test failure rates.** For Git, Perl5, Voldemort only (1,694
merges with runnable test suites): *"76% of merges completed cleanly, 16% of merges
resulted in a textual conflict (TEXTUAL), 1% of merges resulted in a build failure
(BUILD), and 6% of merges resulted in a test failure (TEST)."* Counts: 266 textual,
24 build, 109 test, 1,295 clean; these sum to 1,694.

> **FLAG - an error in the source that has propagated.** The paper then writes:
> *"The 266 textual conflicts reported by the version control system only represent
> 67% of all conflicts. Stated another way, 33% of the 399 merges that the version
> control system reported as being a clean merge, actually were a build or test
> conflict."*
>
> The arithmetic shows 399 is the count of **all conflicts** (266 textual + 24 build
> + 109 test = 399), **not** the count of clean merges. 133/399 = 33.3%, which is
> the number they computed. But clean merges number 1,295, and textually-clean
> merges number 1,428 - neither is 399. So the widely repeated claim "33% of clean
> merges have build or test conflicts" is a misreading of a garbled sentence in the
> original. **The correct clean-merge higher-order failure rate is 133/1,428 =
> 9.3%.**
>
> This error has propagated. Kasi & Sarma repeat it nearly verbatim: *"They found
> that in the three open source projects studied, 33% of the 399 merges that the
> version control system reported as being a clean merge, resulted in an indirect
> (build or test) conflict."* If our draft cites the 33% figure, it should either be
> corrected to 9.3% or quoted with the discrepancy noted.

### 1.5 The remaining studies, pinned

**Kasi, B. K., & Sarma, A. (2013).** Cassandra: Proactive conflict minimization
through optimized task scheduling. *ICSE 2013: Proceedings of the 35th
International Conference on Software Engineering*, 732–741.
DOI: 10.1109/ICSE.2013.6606619.
Confirmed at: https://epiclab.github.io/publications/icse13-kasi.pdf

Population: four GitHub projects. Method verbatim: *"We identify conflicts in each
project by recursively integrating developer changes into a shadow master
repository... we first integrate local commits and if the Git merge fails then we
flag that merge as a failure. If the merge is successful then we run build scripts
on those 'clean merges'; if the build is successful then we run test cases."*

Table I as published:

| Project | KLOC | Devs | Changesets | Merges | Total conflicts | Merge conflicts | Build failures | Test failures |
|---|---|---|---|---|---|---|---|---|
| Perl | 2,213 | 51 | 23,079 | 185 | 74 (40%) | 14 (7.6%) | 4 (2.1%) | 56 (30.2%) |
| Storm | 60 | 24 | 975 | 88 | 39 (44%) | 17 (19.3%) | 9 (10.2%) | 13 (14.7%) |
| Jenkins | 565 | 100 | 14,627 | 505 | 204 (54%) | 68 (13.5%) | 74 (14.7%) | 28 (5.6%) |
| Voldemort | 171 | 33 | 3,026 | 380 | 170 (34%) | 55 (14.5%) | 16 (4.2%) | 133 (35%) |

Abstract verbatim: *"In the projects analyzed merge conflicts ranged from 7.6% to
19.3%. Of the clean merges 2.1% to 14.7% had build failures, and 5.6% to 35% of
correct builds incurred test failures."*

> **FLAG - internal inconsistency in Kasi & Sarma.** The body text of Section III
> says *"Merge conflicts ranged from 4.2% to 19.3%"*, but 4.2% is Voldemort's
> **build** failure rate, not a merge conflict rate. The abstract's 7.6%–19.3% is
> the correct range for merge conflicts. Cite the abstract range.

Note also that Ghiotto et al. characterise Kasi & Sarma as reporting *"high
percentages of merge failures (40%, 44%, 34%, and 54%)"* - those are the **total**
conflict rates including build and test failures, not merge conflicts. Anyone
citing "40–54% conflict rates" from this paper is citing the combined figure.

**Zimmermann, T. (2007).** Mining Workspace Updates in CVS. *MSR '07: Proceedings
of the Fourth International Workshop on Mining Software Repositories*, 11.
DOI: 10.1109/MSR.2007.22.
Confirmed at: https://dl.acm.org/doi/abs/10.1109/MSR.2007.22 and full text at
https://thomas-zimmermann.com/publications/files/zimmermann-msr-2007.pdf

Single author (not "et al."). Definitions verbatim from the paper:
Integration Rate = `(G+C)/(W+U+P+G+C)`; Conflict Rate = `C/(G+C)`, where G is a
file integrated without conflicts and C a file integrated with conflicts, both
being per-file CVS history events. Table 2:

| | GCC | JBOSS | JEDIT | PYTHON |
|---|---|---|---|---|
| Integration rate `(G+C)/(W+U+P+G+C)` | 0.26% | 0.15% | 0.54% | 0.43% |
| **Conflict rate `C/(G+C)`** | **22.75%** | **46.62%** | **24.32%** | **38.26%** |
| Commits (M, A) that led to integrations | 9.06% | 3.89% | 9.03% | 20.20% |
| **Commits (M, A) that led to conflicts** | **2.84%** | **1.86%** | **2.58%** | **7.82%** |

Paper's own gloss: *"the conflict rate that measures the frequency of conflicts is
between 22.75% (for GCC) and 46.62% (for JBOSS)."*

**This is the origin of the highest numbers in the literature, and it is the most
restrictive denominator in the literature.** Brun et al. cite it as *"of all merges,
23% to 47% had textual conflicts"* - but Zimmermann's denominator is not "all
merges", it is "all integrations", i.e. per-file update events where a merge was
already required. Zimmermann's commit-level conflict rate, in the same table, is
1.86%–7.82%.

**Accioly, P., Borba, P., & Cavalcanti, G. (2018).** Understanding semi-structured
merge conflict characteristics in open-source Java projects. *Empirical Software
Engineering*, 23(4), 2051–2085. DOI: 10.1007/s10664-017-9586-1.
Confirmed at: https://link.springer.com/article/10.1007/s10664-017-9586-1 and
author copy at https://pauloborba.cin.ufpe.br/publication/2018understanding_semi-structured_merge_conflict_characteristics_in_open-source_java_projects/2018ESESemistructuredMergeConflictCharacteristics.pdf

Verbatim: *"From the 70,047 analyzed merge scenarios, 4,141 (total of 5.91%, with a
median of 4.43%, and an IQR of 5.54%) contain conflicts in Java Files. In these
scenarios, 28,883 conflicts were detected."* 123 GitHub Java projects. This is the
**semistructured** (FSTMerge) rate; the paper deliberately avoids unstructured
tools for conflict derivation because of spurious conflicts. Removing spacing and
consecutive-line-edit conflicts is discussed as moving the total conflicting
scenario rate to 8.39%.

> Minor discrepancy: the Springer page of record reports the median as 6.64%; the
> author preprint reports 4.43%. The 5.91% total and 70,047 denominator agree in
> both. Use 5.91%; flag the median if it is load-bearing.

Per-project conflict rates in this paper range from **0.97% (Javaee7-samples) to
42.21% (Hive)**, with Hystrix at 1.66%. Per-project variance dwarfs the
between-study variance - worth citing when defending our own figure.

**Leßenich, O., Siegmund, J., Apel, S., Kästner, C., & Hunsen, C. (2018).**
Indicators for merge conflicts in the wild: survey and empirical study. *Automated
Software Engineering*, 25(2), 279–313. DOI: 10.1007/s10515-017-0227-0.
Confirmed at: https://link.springer.com/article/10.1007/s10515-017-0227-0 and
https://dblp.org/rec/journals/ase/LessenichSAKH18.html

163 Java projects, 21,488 merge scenarios from two-parent commits. Verbatim:
*"we found that in 2361 of the 21,488 merge scenarios Git reports a merge conflict.
Considering only actual Java code... Git reports a merge conflict in 1379 of the
21,488 merge scenarios."* = 10.99% and 6.41%. Headline finding: none of the seven
developer-nominated indicators predicts conflict frequency.

**Owhadi-Kareshk, M., Nadi, S., & Rubin, J. (2019).** Predicting Merge Conflicts in
Collaborative Software Development. *ESEM 2019: ACM/IEEE International Symposium on
Empirical Software Engineering and Measurement*. arXiv:1907.06274.
Confirmed at: https://arxiv.org/abs/1907.06274 and
https://github.com/ualberta-smr/conflict-prediction

Verbatim: *"Out of 267,657 scenarios, 21,734 have at least one conflict in their
textual files, such as code files or documentation files. In our data, the conflict
rate across the different programming languages is 8.12%."* 744 repositories, seven
languages. Note their numerator counts conflicts in **any** textual file including
documentation, which inflates relative to a code-only count.

---

## 2. The 2026 agent-coordination systems, pinned

### Claim Plane
**Title:** Claim Plane: Enforceable Change Intents and Dynamic Scope for Parallel
Coding Agents
**Authors:** Maxim Nikolaev (single author)
**arXiv:** 2607.21909 · **v1 submitted:** 2026-07-24
Confirmed at: https://arxiv.org/abs/2607.21909

Treats concurrent change as a pre-write admission problem. Each worker declares a
versioned ChangeIntent (exact base commit, typed resources, dependencies,
operations marked committed or contingent) before implementing. A deterministic
control plane atomically admits compatible intents, constrains same-file
parallelism to declared regions, serialises unresolved overlap, tracks dependency
invalidation, and fails closed on ambiguous authority. Evaluation: six pairs passed
with full serialisation; dynamic scope retained parallel admission on half the
pairs, with seven successful scope promotions and two failed mutations.

### ATM
**Title:** ATM: CID-Brokered Pre-Write Admission for Multi-Agent Code Co-Synthesis
**Authors:** Eagl Huang (single author - verified three ways: abs page, arXiv export
API, and the paper's own HTML title block)
**arXiv:** 2607.00041 · **v1 submitted:** 2026-06-29
Confirmed at: https://arxiv.org/abs/2607.00041 and https://arxiv.org/html/2607.00041v1

**ATM expands to "AI-Atomic-Framework"** - not letter-for-letter, and *not* "Agent
Transaction Manager". "CID" is Content Identifier. Adapter-guided atomization maps
write intents onto semantic atoms and bounded regions; a CID broker routes them
through admission gates covering CID identity, shared surfaces, read/write
dependencies, file ranges, ConflictKey overlap, and base-hash validation. Approved
mutations are applied by a neutral steward, not the proposing agent.

Evaluation structure (no headline conflict-rate percentage is reported): a
12-scenario deterministic design matrix; ATM-AdmissionBench with 20 unique
scenarios and 42 mode-level comparisons; three archived runner cases; three
archived same-file boundary cases; a three-week external-adopter study. The paper
explicitly declines a superiority claim: *"these results support feasibility within
the observed single-domain settings, but not broad comparative superiority over
alternative concurrency-control systems."*

### CoAgent
**Title:** CoAgent: Concurrency Control for Multi-Agent Systems
**Authors:** Hongtao Lyu, Dingyan Zhang, Mingyu Wu, Xingda Wei, Haibo Chen
(Shanghai Jiao Tong University)
**arXiv:** 2606.15376 · **v1 submitted:** 2026-06-13 · only version
Confirmed at: https://arxiv.org/abs/2606.15376 and https://arxiv.org/html/2606.15376v1

> Name caveat: the second author is **Dingyan** Zhang per the arXiv API and the
> paper's own HTML title block (email `healthcliff-ding@sjtu.edu.cn`). One rendering
> of the abs page showed "Dingdan". Two of three sources including the paper itself
> give Dingyan. Worth one human glance if load-bearing.

Database-style concurrency control for LLM agents on a shared git tree. Protocol
**MTPO (Monotonic Trajectory Pre-Order)** fixes a serialisation order at launch,
serves each read the order-filtered value, and applies writes speculatively in
place; a one-way notification asks an affected reader to re-judge and patch its
plan, with saga-style inverses undoing misplaced writes. Control is advisory:
"the runtime informs, the agent repairs."

Results verbatim: *"On ten contended workloads, CoAgent stays within 5% of serial
correctness at a 1.4x speedup and near-serial token cost, where 2PL and OCC
surrender nearly all concurrency gains; on a bash-only target system, it grows a
25-tool library online and lifts the task pass rate from 45/71 to 63/71 at 0.80x
the time and 0.86x the cost."*

### STORM - including the file-granularity false rejection sentence
**Title:** Multi-agent Collaboration with State Management
**Authors:** Mengyang Liu, Taozhi Chen, Zhenhua Xu, Xue Jiang, Yihong Dong
**arXiv:** 2605.20563 · **v1 submitted:** 2026-05-19 · only version
Confirmed at: https://arxiv.org/abs/2605.20563 and https://arxiv.org/html/2605.20563v1

> **Two corrections for the draft.** (a) **"STORM" does not appear in the title.**
> It is the system name, expanding to **STate-ORiented Management**, defined in the
> abstract as *"STORM, i.e., STate-ORiented Management for multi-agent
> collaboration"*. Cite it as Liu et al., *Multi-agent Collaboration with State
> Management*. (b) It is **not** the Stanford NLP writing STORM (Shao et al.);
> this one is evaluated on Commit0 and PaperBench.

**The requested sentence. Location: Appendix E, "Limitations", third limitation
paragraph, under the run-in heading "File-level granularity."** Verbatim:

> **File-level granularity.** Two agents editing different functions in the same
> file trigger a false-positive rejection. Heavily shared files (e.g.,
> `__init__.py`) become serialization bottlenecks. Line-level or hunk-level
> tracking would reduce this at the cost of managing shifting offsets after each
> edit.

> **Wording caveat:** the paper says **"false-positive rejection"**, not "false
> rejection". Quote it as written.

This is the single most useful sentence in the 2026 literature for our
motivation - it is a direct, first-party admission by a coordination system that
file-granularity claiming produces spurious rejections, and it names line- or
hunk-level tracking (not byte-level) as the unexplored remedy, with offset drift as
the stated cost. Our byte-range approach answers exactly the problem this sentence
poses.

Supporting statistics: write-attempt acceptance is 91.8% for the GitWorktree
baseline (3.81 attempts/run), 81.2% for STORM at k=4 (6.00 attempts/run), and 67.1%
at k=8 (10.25 attempts/run) - rejection rates of 8.2% / 18.8% / 32.9%. First-round
task overlap: 21.7% of first-round tasks overlap another's file scope at k=4,
rising to 35.1% at k=8. Single-file task share 91.4% (k=4), 85.1% (k=8).
*"First-round overlap correlates with rejected-review rate (r=0.28 overall, r=0.78
within k=8)."* Headline: +18.7 on Commit0-Lite, +1.4 on PaperBench over the
git-worktree baseline.

### Shepherd and CooperBench - TWO SEPARATE PAPERS
Shepherd is a runtime substrate that *uses* CooperBench as an evaluation.
CooperBench is a separate benchmark by a different group, cited as reference [17]
in Shepherd. Do not conflate them.

**Shepherd:** Enabling Programmable Meta-Agents via Reversible Agentic Execution
Traces. Simon Yu, Derek Chong, Ananjan Nandi, Dilara Soylu, Jiuding Sun,
Christopher D. Manning, Weiyan Shi. arXiv:2605.10913. v1 2026-05-11, v2 2026-05-25,
**v3 (current) 2026-06-24**.
Confirmed at: https://arxiv.org/abs/2605.10913 · https://shepherd-agents.ai/

> v1 carried a different title (*"Shepherd: A Runtime Substrate Empowering
> Meta-Agents with a Formalized Execution Trace"*). Cite the v3 title.

**Supervisor results** (Section 5.1, "Meta-Agent for Multi-Agent Coordination:
Runtime Supervisor"), denominator = 479 CooperBench pairs, Haiku 4.5 workers:

| Condition | Pair pass rate | Wall clock / pair |
|---|---|---|
| solo (1 agent, both features sequentially) - ceiling | 57.2% | 28.4 min |
| coop, no supervisor - unsupervised baseline | 28.8% | 19.8 min |
| + Sonnet supervisor meta-agent | 45.3% | 21.2 min (1.4 min overhead) |
| + Opus supervisor meta-agent | **54.7%** | 24.2 min (4.3 min overhead) |

Verbatim: *"On the full 479-pair set, the coop baseline lands at 28.8% pair pass
rate, reproducing CooperBench's documented coordination penalty against the solo
ceiling of 57.2%, showing a 28.4-point gap. A Sonnet meta-agent recovers 45.3%, and
an Opus meta-agent reaches 54.7%, closing 91% of the curse-of-coordination gap."*
Supervisor actions: *inject*, *handoff*, *discard*.

**CooperBench:** Why Coding Agents Cannot be Your Teammates Yet. Arpandeep Khatua,
Hao Zhu, Peter Tran, Arya Prabhudesai, Frederic Sadrieh, Johann K. Lieberwirth,
Xinkai Yu, Yicheng Fu, Michael J. Ryan, Jiaxin Pei, Diyi Yang. arXiv:2601.13295.
v1 2026-01-19, v2 2026-01-26.
Confirmed at: https://arxiv.org/abs/2601.13295 · https://cooperbench.com/ ·
https://github.com/cooperbench/CooperBench

652 collaborative coding tasks, 12 libraries, 4 languages. Headline "curse of
coordination": two agents score on average **30% lower** together than one agent
doing both tasks. Notably for us: communication *"does significantly reduce the
merge conflicts between patches"* for several models, **but that reduction did not
translate into higher task success** - a caution against treating conflict
avoidance as a sufficient objective.

> OpenReview lists a title variant, *"CooperBench: Benchmarking Cooperation in
> Coding Agents"* (https://openreview.net/forum?id=AomNqiSwb1). Check which venue
> you are citing.

---

## 3. Paste-ready reference entries

### Concurrency control

Kung, H.T., & Robinson, J.T. (1981). On optimistic methods for concurrency control.
*ACM Transactions on Database Systems*, 6(2), 213–226.
https://doi.org/10.1145/319566.319567
Confirmed at: https://dblp.org/rec/journals/tods/KungR81.xml and
https://api.semanticscholar.org/graph/v1/paper/DOI:10.1145/319566.319567
(ACM DL returns 403 to automated fetch; two independent indexes agree on 6(2),
213–226, June 1981. Robinson's full given name is John T.)

### Co-change and change impact

Zimmermann, T., Weißgerber, P., Diehl, S., & Zeller, A. (2004). Mining version
histories to guide software changes. In *Proceedings of the 26th International
Conference on Software Engineering (ICSE '04)*, 563–572. IEEE Computer Society.
https://doi.org/10.1109/ICSE.2004.1317478
Confirmed at: https://www.st.cs.uni-saarland.de/publications/details/zimmermann-icse-2004/

Zimmermann, T., Zeller, A., Weissgerber, P., & Diehl, S. (2005). Mining version
histories to guide software changes. *IEEE Transactions on Software Engineering*,
31(6), 429–445. https://doi.org/10.1109/TSE.2005.72
Confirmed at: https://dl.acm.org/doi/abs/10.1109/TSE.2005.72 and
https://thomas-zimmermann.com/publications/files/zimmermann-tse-2005.pdf

> **Distinguishing the two:** same title, **different author order**. ICSE 2004 is
> Zimmermann, Weißgerber, Diehl, Zeller. TSE 2005 is Zimmermann, **Zeller**,
> Weissgerber, Diehl. Also note Weißgerber (eszett) on the ICSE paper vs
> Weissgerber (ss) on the TSE paper; "Weissgerber" is the safer ASCII form, applied
> consistently.

### The Rolfsnes/Moonen disambiguation - RESOLVED, and the brief was wrong twice

**The 39-measures paper and the confidence/support conclusion are the SAME paper:
ASE 2016 - and its first author is MOONEN, not Rolfsnes.**

Moonen, L., Di Alesio, S., Binkley, D.W., & Rolfsnes, T. (2016). Practical
guidelines for change recommendation using association rule mining. In *Proceedings
of the 31st IEEE/ACM International Conference on Automated Software Engineering
(ASE 2016)*, 732–743. https://doi.org/10.1145/2970276.2970327
Confirmed at: https://dblp.org/pid/165/9613.xml ·
https://ieeexplore.ieee.org/document/7582809/ · full text at
https://web-backend.simula.no/sites/default/files/publications/files/practical_guidelines_for_change_recommendation_using_association_rule_mining_1.pdf

The 39 measures, Section 4.2 ("Interestingness Measures"), verbatim: *"In this
paper, we consider 39 interestingness measures commonly used in several data mining
and machine learning applications."* Table 2 caption: *"Overview of the 39
interestingness measures considered in our study"*.

**The confidence/support conclusion**, Section 5.2 ("Impact of Interestingness
Measures"), answering RQ1, verbatim:

> *"Tukey's honestly significant difference (HSD) test finds that eleven measures
> populate the top equivalence class. These are shown in Table 6."*
>
> *"Also observe that Table 6 includes the classic measures confidence and support.
> Their presence reinforces a result from the recent work of Le and Lo."*
>
> *"Thus in summary for RQ1, we find that to a large extent measure's influence on
> average precision is consistent across differences in other variables and that the
> traditional measures are top performers."*

Table 6, all eleven in equivalence group `a`: casual confidence 0.446, klosgen
0.446, descriptive confirmed confidence 0.446, added value 0.445, collective
strength 0.445, loevinger 0.445, **confidence 0.444**, leverage 0.443, example and
counterexample rate 0.443, difference of confidence 0.443, **support 0.442** - "not
statistically different from each other."

> Cite by section number, not page: the Simula preprint's pagination does not match
> the published pp. 732–743.

**The other two 2016 papers, which are NOT the 39-measures paper:**

Rolfsnes, T., Di Alesio, S., Behjati, R., Moonen, L., & Binkley, D.W. (2016).
Generalizing the analysis of evolutionary coupling for software change impact
analysis. In *2016 IEEE 23rd International Conference on Software Analysis,
Evolution, and Reengineering (SANER)*, 201–212.
https://doi.org/10.1109/SANER.2016.101
Confirmed at: https://dblp.org/pid/165/9613.xml
(This is the TARMAQ algorithm paper. It does not evaluate 39 measures.)

Rolfsnes, T., Moonen, L., Di Alesio, S., Behjati, R., & Binkley, D.W. (2016).
Improving change recommendation using aggregated association rules. In *Proceedings
of the 13th International Conference on Mining Software Repositories (MSR '16)*,
73–84. https://doi.org/10.1145/2901739.2901756
Confirmed at: https://dblp.org/pid/165/9613.xml

> **Correction to the brief:** "Improving Change Recommendation using Aggregated
> Association Rules" is **MSR 2016, not ASE 2016**. Three distinct 2016 papers are
> in play, not two.

**The two 2018 EMSE extensions - neither extends the ASE 2016 paper:**

Rolfsnes, T., Moonen, L., Di Alesio, S., Behjati, R., & Binkley, D.W. (2018).
Aggregating association rules to improve change recommendation. *Empirical Software
Engineering*, 23(2), 987–1035. https://doi.org/10.1007/s10664-017-9560-y
(extends MSR 2016)

Moonen, L., Rolfsnes, T., Binkley, D.W., & Di Alesio, S. (2018). What are the
effects of history length and age on mining software change impact? *Empirical
Software Engineering*, 23(4), 2362–2397. https://doi.org/10.1007/s10664-017-9588-z
(extends SCAM 2016)

Confirmed at: https://dblp.org/pid/165/9613.xml and
https://link.springer.com/article/10.1007/s10664-017-9560-y

> **If you need the 39-measures or confidence/support result, ASE 2016 is the only
> citable source - it has no journal extension.**
>
> Binkley is rendered "Dave W. Binkley" on some DBLP entries and "David Binkley" on
> the ASE 2016 entry; the ASE PDF byline reads "Dave Binkley". Normalized to
> **Binkley, D.W.** throughout.

### Patch theory

Mimram, S., & Di Giusto, C. (2013). A categorical theory of patches. *Electronic
Notes in Theoretical Computer Science*, 298, 283–307.
https://doi.org/10.1016/j.entcs.2013.09.018 · arXiv:1311.3903 [cs.LO]
Confirmed at: https://dblp.org/rec/journals/entcs/MimramG13.html ·
https://www.sciencedirect.com/science/article/pii/S1571066113000649
(MFPS XXIX proceedings, published as an ENTCS volume; cite the ENTCS form.)

Angiuli, C., Morehouse, E., Licata, D.R., & Harper, R. (2014). Homotopical patch
theory. In *Proceedings of the 19th ACM SIGPLAN International Conference on
Functional Programming (ICFP '14)*, 243–256.
https://doi.org/10.1145/2628136.2628158
Confirmed at: https://dblp.org/search/publ/api?q=Homotopical+Patch+Theory

Angiuli, C., Morehouse, E., Licata, D.R., & Harper, R. (2016). Homotopical patch
theory. *Journal of Functional Programming*, 26, e18.
https://doi.org/10.1017/S0956796816000198
Confirmed at:
https://www.cambridge.org/core/journals/journal-of-functional-programming/article/homotopical-patch-theory/42AD8BB8A91688BCAC16FD4D6A2C3FE7
and https://carloangiuli.com/

> A JFP version exists and **the first author's own page marks it as preferred**
> over the proceedings version. Prefer JFP if citing only one. It is
> article-numbered (e18), not page-ranged.
>
> Two DOIs circulate for the ICFP paper: `10.1145/2628136.2628158` (proceedings) and
> `10.1145/2692915.2628158` (the *ACM SIGPLAN Notices* 49(9) reprint). Both resolve.

### Darcs - NO PEER-REVIEWED PUBLICATION EXISTS

**There is no peer-reviewed paper by the Darcs authors presenting Darcs patch
theory.** The theory lives in project documentation; the refereed literature about
it was written by outsiders formalizing it afterwards. Darcs' own bibliography page
lists no Roundy publication.
Confirmed at: https://darcs.net/Theory and https://darcs.net/Theory/Bibliography

Citable options, in descending academic weight:

1. **Peer-reviewed proxies:** Mimram & Di Giusto (2013) and Angiuli et al.
   (2014/2016), both listed on darcs.net/Theory as formalizations of Darcs patch
   theory. Angiuli et al. is the closer fit - it formalizes Darcs patches directly.
2. **Technical report:** Jacobson, J. (2009). *A formalization of Darcs patch theory
   using inverse semigroups* (Technical Report CAM report 09-83). University of
   California, Los Angeles.
   **Both distribution URLs are dead:** `www.math.ucla.edu/~jjacobson/patch-theory/`
   → 404; `ftp.math.ucla.edu/pub/camreport/cam09-83.pdf` → 403. Existence and
   identifiers confirmed via the Darcs bibliography and the darcs-users announcement
   (https://lists.osuosl.org/pipermail/darcs-users/2009-October/022004.html), but the
   document itself was not retrieved. Cite without a live URL or find an archive.
3. **Project documentation:** The Darcs Team. *Darcs theory*. https://darcs.net/Theory
   (accessed [date]). Live and fetchable; this is what most informal citations point
   at. Note `darcs.net/manual/` → 500 and `darcs.net/Using/Manual` → 404, so use
   `/Theory`.
4. **Expository, not peer-reviewed:** Dagit, J. (2009). Darcs patch theory. *The
   Monad.Reader*.
   https://www.cs.tufts.edu/comp/150GIT/archive/jason-dagit/tmr-darcs.pdf

> **Recommendation:** cite Angiuli et al. (JFP 2016) for the theory and
> darcs.net/Theory as a URL for the system. Do not imply a peer-reviewed Darcs paper
> exists.

### Pijul - NO ACADEMIC PUBLICATION EXISTS

**A DBLP full-text search for "Pijul" returns zero hits** (total 0, computed 0,
sent 0). No paper, no technical report.
Confirmed at: https://dblp.org/search/publ/api?q=Pijul&format=json

Pijul's own manual cites exactly one external academic work (a homomorphic hashing
ePrint) and no Pijul-authored publication. Its theoretical lineage is **borrowed**
from Mimram & Di Giusto - merge as pushout.
Confirmed at: https://pijul.org/manual/theory.html

Citable options:

1. **The theory it is built on:** Mimram & Di Giusto (2013), above. The only
   peer-reviewed citation honestly attachable to Pijul's theory.
2. **The project as a web resource:** Meunier, P.-É., & Becker, F. *Pijul: A
   distributed version control system based on a theory of patches*.
   https://pijul.org (accessed [date]); manual theory page
   https://pijul.org/manual/theory.html (live and fetchable).
3. **A conference tutorial, NOT a paper:** Meunier, P.-É. (2018). *Pijul, a purely
   functional version control system* [Tutorial T09]. ICFP 2018, St. Louis, MO,
   29 September 2018.
   https://icfp18.sigplan.org/details/icfp-2018-Tutorials/9/T09-Pijul-a-purely-functional-version-control-system-
   This is a session listing, not a refereed publication. Do not present it as one.

> **Recommendation:** cite Pijul as a URL and Mimram & Di Giusto for its theory. Do
> not attribute a paper to Meunier.

### Pseudo-tested methods and extreme mutation

Niedermayr, R., Juergens, E., & Wagner, S. (2016). Will my tests tell me if I break
this code? In *Proceedings of the International Workshop on Continuous Software
Evolution and Delivery (CSED '16)*, 23–29. ACM.
https://doi.org/10.1145/2896941.2896944 · arXiv:1611.07163
Confirmed at:
https://dblp.org/search/publ/api?q=Will+my+tests+tell+me+if+I+break+this+code ·
http://arxiv.org/abs/1611.07163 · full text
https://teamscale.com/hubfs/Publications/2016-will-my-tests-tell-me-if-i-break-this-code.pdf

Venue confirmed as **CSED@ICSE 2016** (co-located with ICSE 2016, Austin, TX).

**Pseudo-tested, in one sentence:** a method is pseudo-tested if it is covered by
the test suite yet no test fails when its entire body is removed - the tests execute
it but verify nothing about what it does.

> DBLP renders the second author **Jürgens**; the published byline reads
> **Juergens**. Use Juergens, E.

Vera-Pérez, O.L., Monperrus, M., & Baudry, B. (2018). Descartes: A PITest engine to
detect pseudo-tested methods - Tool demonstration. In *Proceedings of the 33rd
ACM/IEEE International Conference on Automated Software Engineering (ASE 2018)*,
908–911. https://doi.org/10.1145/3238147.3240474 · arXiv:1811.03045
Confirmed at: https://dl.acm.org/doi/10.1145/3238147.3240474 ·
https://arxiv.org/abs/1811.03045

Vera-Pérez, O.L., Danglot, B., Monperrus, M., & Baudry, B. (2019). A comprehensive
study of pseudo-tested methods. *Empirical Software Engineering*, 24(3), 1195–1225.
https://doi.org/10.1007/s10664-018-9653-2 · arXiv:1807.05030
Confirmed at: https://link.springer.com/article/10.1007/s10664-018-9653-2

> **Two corrections to the brief.** (a) The EMSE study is **2019**, volume 24 issue
> 3 - online-first in 2018, hence the widely-seen wrong year. (b) It has a **fourth
> author, Benjamin Danglot**, who is not on the tool paper. The two author lists
> genuinely differ. Note the accent: Vera-Pérez.

### Verified in the course of sections 1 and 4

Musco, V., Monperrus, M., & Preux, P. (2017). A large-scale study of call
graph-based impact prediction using mutation testing. *Software Quality Journal*,
25(3), 921–950. https://doi.org/10.1007/s11219-016-9332-8 · arXiv:1812.06286
Confirmed at: https://link.springer.com/article/10.1007/s11219-016-9332-8

Maddila, C., Nagappan, N., Bird, C., Gousios, G., & van Deursen, A. (2021). ConE: A
Concurrent Edit Detection Tool for Large Scale Software Development.
arXiv:2101.06542v3, 25 Sep 2021. Confirmed at: https://arxiv.org/pdf/2101.06542

Ogenrwot, D., & Businge, J. (2026). AgenticFlict: A Large-Scale Dataset of Merge
Conflicts in AI Coding Agent Pull Requests on GitHub. arXiv:2604.03551v2,
12 May 2026. Confirmed at: https://arxiv.org/html/2604.03551v2

---

## 4. Novelty audit

### (a) Conflicted regions measured in bytes, with byte-range intersection between the merge sides

**None found.**

Search terms used: `merge conflict study "byte range" OR "byte-level" intersection
overlapping edit regions empirical measurement`; `"concurrent edits" same file "did
not conflict" fraction empirical study counterfactual file-level locking too
coarse`; plus direct full-text search for "byte" across the extracted text of
Ghiotto et al. 2018, Accioly et al. 2018, Brun et al. 2011, and Kasi & Sarma 2013  - 
**zero occurrences in all four**.

The nearest approaches and why each falls short:

- **Ghiotto et al. 2018** is the closest prior art and the one the claim must
  survive. They measure chunk size **in LOC**, separately for each of the two
  versions, over 175,805 chunks. But the chunk is git's output boundary, taken as
  given; they never compute an intersection between the two sides' edit ranges, and
  they never use a sub-line unit. Their scripts explicitly *"ignore formatting
  characters such as blank spaces and line breaks"* - i.e. they discard exactly the
  information a byte-level measurement retains.
- **AgenticFlict (2026)** extracts 336K+ "fine-grained conflict regions" but records
  *"line boundaries (start_line, mid_line, end_line)"*. Line-level. Confirmed no
  byte-range intersection: they compute SHA-256 hashes of each side's block and
  short textual previews, with no character- or byte-level span analysis.
- **STORM (2026)** names line-level or hunk-level tracking as the *unexplored*
  remedy to file-granularity false-positive rejection (quoted in section 2) - it
  does not implement even that, let alone byte-level.

**Assessment: the claim holds as stated,** and is strengthened by STORM's Appendix E
naming the gap. Recommended phrasing: rather than "no prior work measures byte-level
conflict anatomy" (unfalsifiable in the limit), say prior anatomy work measures
conflicts in *lines* at git-delimited *chunk* granularity, and cite Ghiotto et al.
for LOC and AgenticFlict for line boundaries.

### (b) The over-block counterfactual: fraction of same-file concurrent edits that did NOT conflict

**None found.**

Search terms used: `empirical study fraction merges both parents modified same file
but merged cleanly without conflict git overlapping file edits benign`;
`"concurrent edits" same file "did not conflict" fraction empirical study
counterfactual file-level locking too coarse`; and a targeted read of ConE.

The nearest approaches:

- **ConE (Maddila et al. 2021)** is the closest, and it is explicitly file-level.
  Its Extent of Overlap metric is *"the percentage of files edited in the reference
  pull request that overlap with each of the active pull requests"*, and the paper
  concedes: *"We calculate the overlap in terms of number of overlapping files for
  now. This can be easily extended to calculate the overlap between two active pull
  requests in terms of number of classes or methods."* ConE **notifies** on
  same-file concurrency; it does **not** report what fraction of those concurrent
  same-file edits would actually have conflicted. No counterfactual.
- **Owhadi-Kareshk et al. 2019** builds conflicting/safe classes over 267,657
  two-parent merges and notes the data is *"highly imbalanced"*, but does not
  condition on same-file concurrent edits and does not report the non-conflicting
  fraction of them.
- **AgenticFlict (2026)**: confirmed no counterfactual - *"purely descriptive of
  observed conflicts, not comparative against hypothetical conflict-free
  scenarios."*

**Assessment: the claim holds.** This appears to be the strongest of the three
novelty claims. It is also the one that directly quantifies the cost STORM names
qualitatively, which makes it a well-motivated contribution rather than a
curiosity.

### (c) Validating change-impact prediction by intervention (perturb-and-retest)

**NOT NOVEL AS STATED. Prior work does exactly this.**

**Musco, V., Monperrus, M., & Preux, P. (2017).** A large-scale study of call
graph-based impact prediction using mutation testing. *Software Quality Journal*,
25(3), 921–950. DOI: 10.1007/s11219-016-9332-8. arXiv:1812.06286.
Confirmed at: https://link.springer.com/article/10.1007/s11219-016-9332-8 and
https://arxiv.org/pdf/1812.06286

Their methodology is perturb-and-retest intervention, verbatim:

> *"we present a novel experimental protocol, inspired from mutation testing.
> [We inject] changes (mutations) in it. When running the test suite, some of the
> test cases fail; we consider the set of such failing test cases as being the
> ground truth that we have to predict. To predict the impact of [the change] we
> compare the ground truth with the prediction. We obtain a confusion matrix from
> which we compute the precision and the recall of each call graph."*

And: *"Running the test cases on mutants produces the actual impact set of failing
tests."* Scale: 10 open-source Java projects, 5 classical mutation operators, 17,000
mutants. Result: graph sophistication increases recall, but *"the most basic call
graph gives the best trade-off between precision and recall."*

They use the standard impact-analysis vocabulary - starting impact set (SIS),
candidate impact set (CIS), actual impact set (AIS), false-negative and
false-positive impact sets - where AIS is obtained by intervention, not by proxy.

**Recommended repair.** The claim cannot be made in general form. Two honest
narrower versions, depending on what our predictor actually is:

1. If our predictor is **co-change / evolutionary-coupling based**: the novelty is
   applying intervention validation to *co-change-derived* impact prediction. Musco
   et al. validated **call-graph** predictors this way. The co-change literature is
   still validated proxy-against-proxy: Moonen et al. (ASE 2016) rank 39
   interestingness measures by **average precision against historical co-change**,
   i.e. the ground truth is itself the co-change record the measures are computed
   from. Zimmermann et al. (ICSE 2004 / TSE 2005) likewise evaluate against
   historical transactions. That narrower claim is defensible, and Musco et al.
   should be cited as the precedent for the *method* while we claim the
   *application*. This framing also gives us a clean motivation: the top-eleven
   equivalence class in Moonen et al. is so tightly bunched (0.442–0.446 average
   precision, "not statistically different from each other") that a proxy-based
   evaluation cannot separate the measures at all - which is itself an argument for
   an interventional ground truth.
2. If the novelty is byte-level or region-level rather than method-level, say so
   explicitly and drop the "by intervention rather than proxy" framing, which is
   the part that is already occupied.

Either way, **Musco, Monperrus & Preux 2017 must be cited.** Presenting
intervention-based impact validation as new would not survive review; Monperrus is
a high-visibility author in this exact area and the paper is in a mainstream
journal.

Related and worth citing alongside: the pseudo-tested-method line (Niedermayr et
al.; Vera-Pérez et al.) is the same intervention family - extreme mutation applied
to ask what the test suite actually observes.

---

## 5. Claims that could NOT be verified

| Item | Status | Search basis |
|---|---|---|
| Jacobson (2009) *A formalization of Darcs patch theory using inverse semigroups*, CAM report 09-83 - the **document itself** | **NOT VERIFIED** (identifiers verified, file not retrieved) | `"formalization of darcs patch theory" Jacobson inverse semigroups pdf`; direct fetches of `www.math.ucla.edu/~jjacobson/patch-theory/` → 404 and `ftp.math.ucla.edu/pub/camreport/cam09-83.pdf` → 403. Existence confirmed only via darcs.net/Theory/Bibliography and the darcs-users announcement |
| A peer-reviewed publication for **Darcs** patch theory | **CONFIRMED NOT TO EXIST** | darcs.net/Theory/Bibliography lists no Roundy publication; the refereed formalizations are by outsiders (Mimram & Di Giusto; Angiuli et al.) |
| Any academic publication for **Pijul** | **CONFIRMED NOT TO EXIST** | DBLP full-text search for "Pijul" returns zero hits (https://dblp.org/search/publ/api?q=Pijul&format=json). ICFP 2018 item is a tutorial session listing, not a refereed paper |
| Shepherd Appendix E: how the 479 evaluated pairs were selected from CooperBench's 652 tasks; supervisor intervention counts by action type | **NOT VERIFIED** | https://arxiv.org/html/2605.10913v3 fetched twice with appendix-targeted prompts; the HTML renderer returned only body-section content. The PDF at https://arxiv.org/pdf/2605.10913 would likely resolve it |
| Ghiotto et al.'s true conflict rate over their own 960,366 replayed merges | **NOT COMPUTABLE from the paper** | The 25,328 numerator is post-filtering (forks and non-Java-conflict projects removed); the 960,366 denominator is pre-filtering. 2.64% is a lower bound only |
| Accioly et al. median conflicting-scenario rate: 4.43% (author preprint) vs 6.64% (Springer page) | **CONFLICTING SOURCES** | Both fetched; the 5.91% total and 70,047 denominator agree in both. Median unresolved |
| CoAgent author 2: "Dingyan" vs "Dingdan" Zhang | **MINOR CONFLICT** | arXiv API and paper HTML give Dingyan; one abs-page rendering gave Dingdan. Two of three sources including the paper itself say Dingyan |
| Brun et al. "the 5,355 merges that developers performed during the development of Git, Perl5, and Voldemort" | **INCONSISTENT WITH THEIR OWN FIGURE 4** | Figure 4 gives 1,362 + 185 + 147 = 1,694 for those three systems, and all four percentage claims in the surrounding prose (76%, 16%, 1%, 6%) check out against 1,694, not 5,355. The 5,355 likely counts something else (possibly pairwise developer relationships evaluated at merge time). Flagged, not resolved |

---

## 6. Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| Zimmermann's 23–47% is conditioned on integrations, not merges | **Very high** | Formula `C/(G+C)` read directly from the paper's own equation and Table 2, with the paper's own commit-level rates (1.86–7.82%) in the same table as a cross-check |
| Ghiotto et al. report no conflict rate of their own | **Very high** | Explicit in the method (*"When the merge did not lead to a conflict, it was discarded"*) and the 10–20% sentence is a citation to [22],[23], both identified |
| Ghiotto et al. do not measure bytes | **Very high** | Zero "byte" occurrences in the full extracted text; LOC stated as the size unit in the research question itself (A2) |
| Brun Figure 4 = 3,562 merges, 564 (16%) textual | **High** | Reconstructed from a scrambled two-column extraction, but every one of the nine rows sums exactly to its stated merge count and the totals reconcile; four independent prose percentages corroborate |
| Brun's "33% of clean merges" is a garbled sentence; correct figure 9.3% | **High** | 266+24+109 = 399 exactly, and 133/399 = 33.3%, so 399 is demonstrably the conflict count. Slight residual doubt only because I am correcting a published sentence from a reconstructed table |
| Kasi & Sarma Table I values | **Very high** | Read directly from the extracted table; abstract range corroborates |
| Leßenich 6.41% / 10.99% over 21,488 two-parent merges | **High** | Verbatim sentence retrieved from the publisher page; not independently cross-checked against the PDF |
| Owhadi-Kareshk 8.12% over 267,657 two-parent merges | **Very high** | Verbatim sentence plus the two-parent selection method, both read from the paper's own text |
| Accioly 5.91% over 70,047 scenarios | **Very high** | Agrees across the Springer page and the author preprint |
| STORM file-granularity quote and its location | **High** | Retrieved twice on independent fetches with identical wording, located to Appendix E; wording caveat ("false-positive") noted |
| Shepherd supervisor numbers | **High** | Verbatim from Section 5.1; the underlying pair-selection method is NOT VERIFIED |
| ATM = "AI-Atomic-Framework", single author | **High** | Confirmed three ways including the arXiv export API |
| Novelty (a) byte-level: none found | **Moderate-high** | Absence of evidence over four full texts plus targeted searches. Cannot be proven exhaustively; recommended phrasing narrowed accordingly |
| Novelty (b) over-block counterfactual: none found | **Moderate-high** | Same basis; ConE checked directly and is file-level with no counterfactual |
| Novelty (c) intervention validation: NOT novel | **Very high** | Direct verbatim methodology match in a mainstream journal paper; this is a positive finding, not an absence argument |
| 39-measures paper = ASE 2016, first author Moonen; confidence and support in top equivalence class | **Very high** | Verbatim quotes from Sections 4.2 and 5.2 plus Table 6 values, read from the Simula full text; DBLP and IEEE Xplore corroborate the bibliographic record |
| "Improving Change Recommendation using Aggregated Association Rules" is MSR 2016, not ASE 2016 | **Very high** | DBLP author page lists all three 2016 papers with distinct venues and DOIs |
| Vera-Pérez EMSE study is 2019, 24(3), with Danglot as fourth author | **Very high** | Springer page of record; author lists differ demonstrably between the tool paper and the study |
| Kung & Robinson 6(2), 213–226 | **High** | ACM DL blocked (403); two independent indexes (DBLP, Semantic Scholar API) agree |
| No peer-reviewed Darcs paper; no Pijul publication at all | **High** | Negative claims, but grounded in the projects' own bibliography pages and a zero-hit DBLP query rather than only in failed searches |
