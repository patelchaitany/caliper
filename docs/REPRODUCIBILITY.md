# The reproducibility guarantee

This document states exactly what Caliper promises, exactly how each promise is
enforced, and exactly where each one stops. A rating authority that overclaims
is worse than no rating authority, so the boundaries here are as important as
the guarantees.

## The constraint we start from

Caliper cannot make the model deterministic, because the API no longer exposes
the parameter that would do it. `temperature`, `top_p` and `top_k` were removed
on Claude Opus 5 and the 4.6+ family; the Python SDK does not accept them and
the API rejects them with a 400. This is asserted in the test suite so it fails
loudly if it ever changes:

```python
def test_temperature_is_never_sent():
    detector(client).detect(FILES, 0, 5)
    assert "temperature" not in client.calls[0]
```

Every design decision below follows from accepting that the detector is a
sampler and cannot be made otherwise.

---

## Tier 1 — Exact

> Same submission bytes + same rubric hash + same model pin + same author
> history ⇒ **byte-identical review**. Zero variance. No model call.

**Mechanism.** A review is keyed by

```
review_id = H("review", content_hash, rubric_hash, model_pin, history_signature)
```

and stored in the ledger under that key. A repeat request is a lookup.

Each component earns its place:

- `content_hash` — over path-sorted file digests, so directory iteration order
  cannot change it.
- `rubric_hash` — over every weight in the rubric. Change any constant and the
  key changes, because the score would have changed.
- `model_pin` — `model@backend:region`. Two scores from different models are
  not comparable, and the pin makes that visible rather than silent.
- `history_signature` — the author's prior rule counts. Recurrence escalates
  penalties, so history is a genuine *input* to the score.

**The subtle part.** `history_signature` excludes prior reviews of *this same
submission*. Without that exclusion, recording a review would change the key
that identifies it, so an identical re-review would never hit cache — and worse,
an author would accumulate recurrence against themselves for one unchanged
submission reviewed twice. This is enforced by test:

```python
def test_history_signature_excludes_the_submission_under_review(ledger):
    before = ledger.history_signature("dev", "same")
    store(ledger, "dev", "same", ["sql_injection"], "r0")
    assert ledger.history_signature("dev", "same") == before
```

**Where it stops.** Tier 1 does not survive a rubric change, a model change, or
new history landing for that author from *other* submissions. All three should
change the score, and all three change the key.

---

## Tier 2 — Stable

> A cold re-run — cache bypassed, detector resampled — lands within a band that
> Caliper measures and reports.

**Mechanism.** Quorum. K independent passes run over the same submission; a
finding is admitted only if at least `ceil(0.6·K)` passes independently
surfaced it. Findings are clustered by

```
fingerprint = H("finding", rule, qualified_symbol, whitespace_normalised_span)
```

Line numbers are deliberately absent. A finding survives reformatting, and an
unrelated edit above it does not turn it into a new finding.

Three properties make the collapse deterministic given the same observations:

1. **Fixed summation order.** Findings are summed in fingerprint order. IEEE-754
   addition is not associative, so an unordered sum can differ in the last bit
   between runs.
2. **Total tie-breaks.** Where passes disagree on severity, the modal value wins
   and ties resolve toward the *milder* reading — a rating authority should not
   resolve its own uncertainty against the author.
3. **Index-keyed concurrency.** Passes run in a thread pool but results are
   stored by pass index, never by completion order.

**Where it stops.** Quorum reduces variance; it does not eliminate it. A finding
sitting near the quorum boundary will flicker between runs, and that moves the
score. Caliper measures this rather than asserting it away:

```console
$ caliper verify PATH --runs 6
```

reports score spread, standard deviation, raw penalty spread, and the fraction
of findings every run agreed on. Raising `--passes` tightens the band at linear
cost. If a submission's findings are genuinely ambiguous, the spread will be
wide — and that is information, not a defect.

---

## Tier 3 — Comparable

> Any two scores carrying the same rubric hash mean the same thing.

**Mechanism.** The score is a pure function. It cannot read a clock, a network,
a random source or a model. Given a finding set, LOC and a rubric it returns one
number, on any machine, forever.

```python
def test_score_is_independent_of_finding_order():
    values = {score_findings(list(p), 300).value
              for p in itertools.permutations(findings)}
    assert len(values) == 1
```

Because it is pure over *stored* findings, any historical review can be replayed
under a new rubric. Changing the rubric is therefore a measurable act — rescore
the corpus, look at what moved — rather than a discontinuity in the record.

**Where it stops.** Across rubric hashes, scores are not comparable, and Caliper
refuses to pretend otherwise: `GET /v1/authors/{author}/history` returns
`comparable: false` and the CLI annotates the trend line rather than drawing a
clean slope through a rubric change.

---

## Grounding, quantified

Grounding is not a reproducibility mechanism, but it is what makes the inputs to
the above worth anything. Every finding must quote real source; the quote is
located in the file before the finding is admitted.

| Outcome | Meaning | Action |
|---|---|---|
| `exact_quote` | quote found within 2 lines of the claim | keep |
| `relocated_quote` | quote found elsewhere in the file | keep, **correct the line numbers** |
| `symbol_span` | ≥60% of quote lines fall inside one symbol | keep, anchor to the symbol |
| *(rejected)* | quote not in the file at all | **delete**, and count it |

The rejection count is surfaced on every review as *detector precision*. It is
the primary signal to watch when changing the model pin, the effort level or the
prompt — a prompt regression shows up here before it shows up in scores.

---

## Threats to the guarantee, and what defends each

| Threat | Defence |
|---|---|
| Model returns a different set each run | Quorum across K passes; variance measured by `caliper verify` |
| Model invents a location | Quote verification; unquotable findings deleted |
| Model invents an entire finding | Same — an invented finding has no quote in the file |
| Model is asked to rate | It never is. There is no score field in the schema. |
| Float addition order | Findings summed in fingerprint order |
| Thread scheduling | Results keyed by pass index |
| Dict / set iteration order | Canonical JSON with sorted keys everywhere a hash is taken |
| Directory iteration order | Paths sorted before hashing a submission |
| Ambiguous import resolution | Ambiguous keys resolve to no edge rather than a guessed one |
| Silent prompt-cache invalidation | System prefix asserted byte-stable across passes in CI |
| Rubric drift | Every weight hashed into `rubric_hash`, carried on every score |
| Model upgrade changing scores silently | `model_pin` carried on every review and in the cache key |
