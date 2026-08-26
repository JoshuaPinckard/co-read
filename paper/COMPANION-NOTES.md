# Companion notes: the sheaf framing

Working notes for the Greenstein conversation. Deliberately NOT in the paper.
The rule applied: formalism enters the paper when it computes something we
could not state without it. This currently names the finding rather than
computing one, so it lives here until someone extracts a computable
obstruction.

## The observation

Byte ranges on a file generate a genuine topology (intervals on a line, not a
finite lattice imitating one). An edit is a section over its support. A
three-way merge is gluing: disjoint sections glue trivially, which is exactly
why git auto-resolves byte-disjoint concurrent edits, and overlapping
sections glue iff they agree on the overlap, which is exactly what a textual
conflict is when they do not. File TEXT forms a sheaf.

The paper's deepest point in one line: program SEMANTICS does not. Whether
the merged program is correct is not determined by its restrictions to a
cover. That is precisely why byte-disjoint edits can merge cleanly and be
wrong together, and why the design keeps a test gate behind fine-grained
claims. Text glues. Meaning does not. The test gate is the price of the
difference.

## Numbers the framing organizes (all measured, all in the paper in plain
language)

- 69.4% of same-file concurrent units are disjoint sections (they glue).
- 81.7% of real conflicts are overlap disagreements (gluing fails on the
  overlap, honestly detected).
- The 0/44 semantic replay and the 11.1% disjoint minority are the sheaf
  failure of semantics: gluing succeeded on text and nothing in the topology
  certifies the glued object's meaning.

## The project worth a whiteboard

Extract the obstruction as an object. The candidate move is the one
Greenstein pointed at in the other paper's context: pass from "these do not
assemble" as an impression to an element of something computable. For text
the obstruction to gluing is concrete (the disagreement on the overlap). For
semantics the interesting question is whether a usable cover exists at all,
i.e. whether any granularity of syntactic regions determines correctness
locally. The measured answer so far is no at byte granularity, and the
symbol-granularity version (tree-sitter regions, def/ref-aware covers) is
the natural next cover to test. If a cover exists where semantic gluing
works, that IS the right claim granularity, computed rather than chosen.

## The program in five words

Both papers measure the same shape. LEAN-Bench: conventions that fill
specification gaps glue within a model family and fail to glue across
families. Blast Radius: text glues across concurrent editors and meaning
does not. The shared sentence: local-to-global failure in LLM systems. That
is the research program, and it is worth more in an SOP than either result
alone.

## Status

Named, not computed. If the symbol-cover experiment or a Grothendieck-style
obstruction ever computes from the 2,401-draw or mined-conflict data, it
graduates to a paper. Until then this stays a note.
