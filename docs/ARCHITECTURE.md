# Architecture

## The organising rule

**The model detects. Code judges.**

This is enforced in the type system, not by convention. `RawFinding` — the only
thing the detector may produce — has no score field, no priority field and no
weight. `Score` is produced by `scoring/rubric.py`, which imports no provider and
cannot reach a model. There is no path through the types by which a number
invented by a language model reaches a user.

Everything else follows from that rule.

---

## Stages

### 1. Normalise — `pipeline.build_submission`

Files are sorted by path, each hashed with its content, and the submission
identified by the digest of those digests. Directory iteration order cannot
affect the identity of a submission.

### 2. Cache — `store/ledger.py`

See [REPRODUCIBILITY.md](REPRODUCIBILITY.md#tier-1--exact). A hit returns the
stored review and makes no model call.

### 3. Structure — `analysis/structure.py`

Two tiers, and the difference is recorded rather than hidden:

- **Python** → stdlib `ast`. Symbols carry exact spans; `exact=True`.
- **Everything else** → declaration-line regexes from the language profile. A
  symbol runs until the next declaration. Crude, stable, sufficient to attribute
  a line to an owner; `exact=False`.

A file that fails to parse still gets reviewed — it just falls back to the
heuristic coordinate system.

Symbols matter because findings are anchored to *symbols*, not line numbers.
That is what makes recurrence tracking survive edits.

### 4. Impact — `analysis/impact.py`

Imports are resolved against the submission's own files to build a directed
graph, and blast radius is the fraction of the submission that transitively
reaches a file:

```
blast_radius(f) = |{g : g transitively imports f}| / (N - 1)     ∈ [0, 1]
```

Bounded, so it composes into the rubric as a multiplier that cannot make a score
unbounded. A lone file scores 0 — correctly, since nothing else can break.

Two resolution details that matter:

- **Suffix matching.** A submission is rarely rooted where its import paths are:
  files collected as `examples/demo/svc/auth.py` are imported as `svc.auth`. All
  trailing sub-paths are candidate keys.
- **Ambiguity is not resolved.** If two files answer to `utils`, the key is
  dropped and no edge is created. Guessing would make the graph depend on
  iteration order, and a wrong edge silently mis-weights a real finding.

### 5. Detect — `providers/claude.py`

The only probabilistic stage. Request shape:

| Part | Choice | Why |
|---|---|---|
| `model` | `claude-opus-5` | Detection quality is the bottleneck on rating quality |
| `output_config.format` | strict `json_schema` | Guaranteed shape; no tool-choice interactions |
| `output_config.effort` | `high` (configurable) | Effort matters more than on prior models |
| `thinking` | `{"type": "adaptive"}` | `budget_tokens` is removed on Opus 5 (400) |
| `system` | 1–2 blocks, `cache_control` on the last | Vertex has **no** automatic caching — placed by hand |
| `messages` | the code, permuted per pass | Volatile content stays after the breakpoint |
| `temperature` | *absent* | Does not exist. See REPRODUCIBILITY.md |
| transport | `.stream()` + `get_final_message()` | Large `max_tokens` needs streaming to avoid HTTP timeouts |

**Pass diversity is order-only, deliberately.** Each pass sees the same files in
a different order, seeded from the submission's content hash so pass 3 is always
the same on every machine. It is tempting to give each pass a *lens* — one for
security, one for performance — but that would break quorum: a security finding
would only ever be seen by the security pass and could never reach a vote
threshold. Position bias is decorrelated; category coverage is not touched.

**Prompt caching.** Render order is `tools` → `system` → `messages`. The system
prompt is byte-stable — no timestamps, no identifiers, conventions sorted
deterministically — and carries a 1h breakpoint. A review runs K passes over the
same prefix within seconds, and repeat submissions from the same team reuse it.
A single interpolated clock would cost a full cache miss on every request, so
prefix stability is asserted in CI:

```python
def test_system_prefix_is_byte_stable_across_passes():
    prefixes = {json.dumps(call["system"], sort_keys=True) for call in client.calls}
    assert len(prefixes) == 1
```

### 5b. Detect — `providers/gemini.py`

The same stage, a different model, and a genuinely different reproducibility
story. Gemini on Vertex exposes `temperature` and `seed`; Claude exposes
neither, because those parameters were removed from the Messages API.

| | Claude backend | Gemini backend |
|---|---|---|
| Sampling control | none — removed from the API | `temperature=0` |
| Seed | not available | derived from the submission hash |
| Structured output | `output_config.format`, falls back to forced tool use | `response_json_schema` |
| Pass diversity | file order only | file order **and** per-pass seed |
| Tier 2 | a measured band | a measured band — see below |

**The seed must vary per pass.** The tempting move is one fixed seed for the
run, and it would be wrong: at `temperature=0` with an identical seed, all K
passes return the same answer, every finding trivially scores K/K votes, and
quorum reports unanimous agreement from what was really a single sample. So the
seed is `hash(submission_content, pass_index)` — different across passes, so
they are independent hypotheses worth voting between; identical across runs, so
the vote reproduces. Deterministic diversity, rather than a choice between the
two.

**Pinning the sampler does not buy reproducibility, and we measured that.**
Across five calls at identical prompt and config there were exactly two
distinct outputs — and the seed did not predict which one arrived: one seed
produced both, and two different seeds produced the same pair. On this workload
`seed` has no observable effect; the variation sits below it, in replicas,
batching and reasoning-token paths. End to end, three cold runs of a three-file
submission spread 8.00 score points, no tighter than a backend with no sampling
controls at all.

Tier 1 stays the only exact guarantee on both backends. That is the argument
for putting determinism in the ledger and the rubric rather than in request
parameters — and the reason the per-pass seeding in `gemini.py` is documented
as insurance rather than as a mechanism.

What *did* hold across all five calls was the finding set: the same three rules
every time, including from the responses whose bytes differed. Caliper
fingerprints by rule, symbol and normalised span, never by prose, so it already
operates at the level that stayed stable.

That both backends share the prompt, the schema, grounding, quorum and the
rubric — differing only in how bytes reach a model — is the practical test of
the claim that the model is a replaceable component.

### 6. Ground — `analysis/grounding.py`

Every finding carries `quoted_source`. It is located in the file, or the finding
does not exist. Four outcomes, three of which keep the finding — because a
model that *observed* a real defect and *miscounted* the line has still done
something useful, and discarding that would throw away signal along with noise.

### 7. Quorum — `analysis/consensus.py`

Cluster by `(rule, symbol, normalised span)`; admit at ≥60% agreement; carry the
vote count forward so the rubric can weight a 3-of-5 finding below a 5-of-5 one.
All tie-breaks are total and resolve toward the milder reading.

### 8. Score — `scoring/rubric.py`

A pure function. See the README for the formula and
[REPRODUCIBILITY.md](REPRODUCIBILITY.md#tier-3--comparable) for what it
guarantees.

### 9. Remember — `store/ledger.py`

SQLite, because a rating authority should be a file you can copy, diff and hand
to an auditor rather than a service you have to trust. Three tables: `reviews`
(the content-addressed cache and the trend line), `finding_history` (recurrence),
`conventions` (institutional knowledge).

---

## Extending Caliper

**A new language.** Add an extension mapping and a `LanguageProfile` in
`languages.py` — declaration regexes and import regexes. Nothing else changes;
structure, impact, grounding and scoring are all language-agnostic. Adding a
real parser later is a matter of returning `exact=True` symbols from
`structure.symbols_of`.

**A new detector.** Implement the `Detector` protocol: one method returning
`DetectionOutcome`. The entire probabilistic surface of the system is that one
method, which is why `ReplayDetector` can stand in for Claude across the whole
test suite.

**A new rubric.** Construct a `Rubric` with different weights and a new version
string. Old reviews replay under it via `pipeline.rescore`, so you can measure
what the change does to a real corpus before adopting it.

**A different store.** `Ledger` is the only module that touches SQL. Its
interface is six methods.

---

## Deliberate non-goals

- **Not a linter.** The detector prompt explicitly excludes formatting, naming
  and style. A formatter already owns those, and reporting them dilutes signal.
- **No auto-fix.** Caliper rates and explains. Applying changes is a different
  trust decision and belongs behind a different confirmation.
- **No per-caller tuning.** The API adds nothing to the pipeline. A rating whose
  answer depends on which endpoint you asked is not a rating.
