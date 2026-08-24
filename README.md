# Caliper

**A reproducible code review authority.** The model detects; code judges.

Caliper reviews code in any language, separates bugs from architectural concerns
from optimisation opportunities, and produces a quality rating that means the
same thing today as it did last month. It remembers every submission, so growth
becomes visible and repeated mistakes become detectable, and it absorbs an
organisation's own review history so its guidance reflects that team's standards
rather than generic advice.

---

## The problem, and the thing that makes it hard

Code review is the primary quality gate in software engineering and the least
scalable one. Solo developers, students and small teams often have no reviewer
at all. Code written at 2am waits until morning; code in a language nobody else
on the team knows waits indefinitely, or ships unreviewed.

Existing tooling covers fragments of this and fits together badly. Linters are
blind to architecture. SAST is noisy enough that teams learn to ignore it. LLM
assistants explain well but hallucinate line numbers, invent issues, and — the
disqualifying part — **return a different verdict on the same file across two
runs**. And all of them are context-blind about impact: a vulnerability in a
throwaway migration script and the identical vulnerability in an auth helper
that forty modules import get reported at the same severity.

So the central technical challenge is not "review code with an LLM". It is:

> **How do you build a reproducible rating authority on top of a probabilistic
> model?**

### You cannot buy this with `temperature=0`

The obvious answer — pin the sampler — is not available. On the current
Messages API, `temperature` **no longer exists**:

```console
$ python -c "import anthropic, inspect; \
    print('temperature' in inspect.signature( \
      anthropic.Anthropic(api_key='x').messages.create).parameters)"
False
```

`temperature`, `top_p` and `top_k` were removed on Opus 5 and the 4.6+ family;
sending them returns a 400. There is no knob. Any architecture that planned to
purchase determinism from the sampler cannot be built today.

Caliper does not try. It treats every model call as **a sample**, and recovers
determinism at the layers where it can actually be guaranteed rather than hoped
for. That constraint is the design.

---

## The architecture

```
  submission (bytes)
        │
   1 ▸  NORMALISE      content-addressed; paths sorted, whitespace-normalised
        │              identical bytes ⇒ identical review key
        │
   2 ▸  CACHE          seen this exact submission, rubric, model pin and author
        │              history before? return the stored review. no model call.
        │
   3 ▸  STRUCTURE      exact AST where we have a parser, declaration heuristics
        │              elsewhere; every symbol tagged with which one it was
        │
   4 ▸  IMPACT         import graph ⇒ transitive dependents ⇒ blast radius
        │              pure graph maths, no model involvement
        │
   5 ▸  DETECT   ◀──── the ONLY probabilistic stage
        │              K independent passes · Claude Opus 5 on Vertex AI
        │              strict JSON schema · adaptive thinking · cached prefix
        │              output: candidate findings. never a score.
        │
   6 ▸  GROUND         every finding must quote real source. the quote is
        │              checked against the file. wrong line numbers are
        │              corrected; unquotable claims are DELETED and counted.
        │
   7 ▸  QUORUM         cluster by (rule, symbol, normalised span) — never by
        │              line number. admit only findings ≥60% of passes agreed on.
        │
   8 ▸  SCORE          pure function: (findings, impact, LOC, rubric) → 0–100
        │              versioned, hashed, replayable. no model output reaches it.
        │
   9 ▸  REMEMBER       persist per author. recurrence escalates. trend emerges.
```

**Exactly one stage calls a model, and it is not allowed to produce a number.**
Everything after step 5 is a pure function of verified observations. That is
what makes a Caliper score defensible: you can recompute it by hand.

---

## What is actually guaranteed

Three tiers, stated precisely, because a rating authority that overclaims is
worse than none.

| Tier | Guarantee | Mechanism |
|---|---|---|
| **1 — Exact** | Same bytes + same rubric + same model pin + same author history ⇒ **byte-identical review**, zero variance, no model call. | Content-addressed ledger |
| **2 — Stable** | A cold re-run varies within a measured, reportable band — never silently. | Quorum across K passes |
| **3 — Comparable** | Any two scores carrying the same rubric hash mean the same thing. | Pure, versioned rubric |

Tier 2 is *measured, not asserted*. `caliper verify` runs the same submission
several times and reports its own variance:

```console
$ caliper verify examples/decent_service --runs 6
  run 1: score  93.95  penalty    6.05  2 findings  (0 below quorum, 0 ungrounded)
  run 2: score  97.34  penalty    2.66  1 findings  (1 below quorum, 0 ungrounded)
  run 3: score 100.00  penalty    0.00  0 findings  (2 below quorum, 0 ungrounded)
  ...
┌──────────────────────────┬────────────────────────────────────────────┐
│ cold-run scores          │ 93.95, 97.34, 100.00, 96.61, 100.00, 97.34 │
│ score spread             │ 6.05 points  (sd 2.08)                     │
│ finding-set stability    │ 0% agreed by every run                     │
│ repeat review identical  │ yes — byte for byte                        │
└──────────────────────────┴────────────────────────────────────────────┘
```

That output is the honest one, not the flattering one. The fixture contains a
genuinely ambiguous finding, and Caliper says so rather than hiding it behind a
confident number.

---

## Grounding: making hallucination inexpressible

Asking a model not to hallucinate line numbers does not work. Caliper makes an
unverifiable claim structurally impossible instead: every finding must carry
`quoted_source`, the verbatim text it refers to, and that text is checked
against the file before the finding is allowed to exist.

| The model claims | Caliper does |
|---|---|
| Correct quote, correct line | keeps it — `exact_quote` |
| Correct quote, **line 417 of a 30-line file** | finds the quote, **corrects the line**, keeps it — `relocated_quote` |
| Lightly reformatted quote | matches ≥60% into one symbol, anchors to the symbol — `symbol_span` |
| Code that is not in the file | **deletes it**, and counts the deletion |

The discard count is reported on every review as *detector precision*. It is the
honest measure of how much of the model's output was real, and it is the number
to watch when changing models or prompts.

---

## Impact: the context every other tool is blind to

The same defect is not the same defect everywhere. Caliper builds the import
graph and weights each finding by how much of the system transitively depends on
the file it lives in.

```
severity  rule                       location                       votes  blast  penalty
critical  hardcoded_credential       svc/auth.py:7                   5/5   0.67    41.40
critical  hardcoded_credential       scripts/one_off_backfill.py:3   5/5   0.00    27.60
```

Identical rule, identical severity, identical confidence. The one in the
authentication helper that two modules import costs **1.50×** the one in a
backfill script nothing references. That ratio is graph maths, not a judgement
call — which means it is the same on every run, and you can check it.

---

## Memory: reviews that are not stateless

Every finding is recorded against its author, keyed by a fingerprint of
`(rule, symbol, whitespace-normalised span)` — never a line number, so a finding
survives reformatting and unrelated edits above it.

```console
$ caliper history grad@team.dev
                      Score history — grad@team.dev
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━┳━━━━━━━━━━┓
┃ When                ┃ Score ┃ Band                   ┃ LOC ┃ Rubric   ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━╇━━━━━━━━━━┩
│ 2026-08-24 09:16:54 │  60.9 │ C — revisions required │  11 │ 661a15b2 │
│ 2026-08-24 09:16:55 │  91.0 │ A — ship               │  12 │ 661a15b2 │
│ 2026-08-24 09:16:57 │ 100.0 │ A — ship               │  15 │ 661a15b2 │
└─────────────────────┴───────┴────────────────────────┴─────┴──────────┘
Trend: +39.1 points across 3 reviews — improving.

      Repeated across submissions
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Rule                    ┃ Times told ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ request_without_timeout │          2 │
│ swallowed_exception     │          2 │
└─────────────────────────┴────────────┘
Findings by category: correctness 5, security 1
```

The fifth time you are told about `swallowed_exception`, Caliper knows it is the
fifth — and the penalty escalates for it (capped, so it never spirals). A trend
line is only drawn through reviews sharing a rubric hash; where it would cross a
rubric change, Caliper says the scores are not comparable instead of drawing it
anyway.

---

## Institutional knowledge

Conventions learned painfully over years live scattered across old pull request
comments. Nobody rereads four years of review history before approving a diff.

```console
$ caliper ingest-history examples/review_history.jsonl
Read 12 comment(s).
┌──────────────────────────┬──────────────────────────────────────────────┬──────────┐
│ errors_wrapped_with_op   │ Wrap returned errors with the operation name │        2 │
│ explicit_outbound_timeout│ Every outbound call carries an explicit …    │        2 │
│ no_module_level_state    │ Stores are injected, never module globals    │        2 │
│ sql_always_parameterised │ Parameterise all SQL, including admin paths  │        2 │
└──────────────────────────┴──────────────────────────────────────────────┴──────────┘
```

Extracted once, stored in a table a human can read and edit, then injected into
the **cached** half of every future detection prompt — so applying an
organisation's entire accumulated standard costs close to nothing per review.

---

## Quickstart

```bash
git clone <this repo> && cd caliper
uv venv && uv pip install -e ".[dev]"
```

No credentials needed to see it work:

```bash
caliper review examples/sample_submission --backend replay --author you@team.dev
caliper verify examples/decent_service --backend replay --runs 6
caliper rubric
```

Against Claude on Vertex AI:

```bash
gcloud auth application-default login
export CALIPER_GCP_PROJECT=your-project-id CALIPER_GCP_REGION=us-central1
caliper review src/ --author you@team.dev
```

Full GCP setup, including Cloud Run deployment: [docs/GCP.md](docs/GCP.md).

### Commands

| Command | What it does |
|---|---|
| `caliper review PATHS` | Review and rate. `--fail-under 70` for CI gating. |
| `caliper verify PATHS` | Measure this system's own reproducibility. |
| `caliper history AUTHOR` | Score trend and repeated mistakes. |
| `caliper ingest-history FILE` | Absorb an org's review comments as conventions. |
| `caliper rubric` | Print the scoring function and its hash. |
| `caliper serve` | HTTP API on `:8080`. |

---

## The rubric

```
penalty  = severity × category
         × (1 + 0.75·blast_radius)          how much depends on this code
         × (0.55 + 0.45·agreement)          how many passes independently saw it
         × confidence                       the model's own hedge, as a tiebreak
         × (1 + 0.15·min(recurrence, 4))    how many times you have been told

score    = 100 − Σpenalty / max(1, (LOC/200)^0.5)
```

Density, not raw count — a 2000-line submission is not doomed for having more
findings than a 50-line one. Every constant lives in one auditable dataclass and
is hashed into `rubric_hash`, which travels with every score. Change any weight
and the hash changes, which is what stops last month's number from quietly
meaning something else today.

Because the rubric is a pure function of stored findings, an entire history of
reviews can be **replayed** under a new rubric — so changing it is a measurable
act, not a break in continuity.

---

## What this does not do

- **It is not a formatter or a linter.** Those own style, and Caliper is
  explicitly instructed to stay out of it — reporting taste dilutes the signal.
- **Tier 2 is a band, not a point.** Cold re-runs of the same code can differ.
  Caliper measures and reports that band; it does not pretend it is zero.
- **Structural analysis is two-tier.** Python is parsed exactly via `ast`;
  other languages use declaration heuristics, and every symbol records which it
  was. Blast radius on a heuristic parse is a good estimate, not ground truth.
- **Scores under different model pins are not comparable.** A model change is a
  rubric-level event and the pin travels with every review so it stays visible.
- **It does not replace a human reviewer** on questions of intent, product fit,
  or whether the change should exist at all.

## Operational notes from real deployment

Verified against live Claude on Vertex AI, not assumed:

- **Vertex org policy can disable structured outputs per model.** Both
  `output_config.format` and `strict: true` on tools are rejected with a 400
  where `constraints/vertexai.allowedPartnerModelFeatures` excludes them.
  Caliper detects that specific rejection and falls back to forced tool use,
  which the same policy permits — so it runs on restricted projects rather
  than not running at all.
- **The same policy can disable prompt caching without raising.** The request
  succeeds and simply caches nothing. Caliper checks the usage counters and
  reports it, because the only other symptom is a much larger bill.
- **Model availability is per project and per region.** A 404 means Model
  Garden enablement, not policy; the same project can serve a model in
  `us-east5` and 404 on `us-central1`.

Details and a probe script: [docs/GCP.md](docs/GCP.md).

---

## Layout

```
src/caliper/
  hashing.py            content addressing, canonical JSON
  models.py             the domain types — the model/judge boundary is here
  prompts.py            byte-stable, cache-safe detection instructions
  pipeline.py           orchestration
  analysis/
    structure.py        AST (Python) and heuristic symbol extraction
    impact.py           import graph, blast radius
    grounding.py        quote verification, the anti-hallucination layer
    consensus.py        quorum across passes
  scoring/rubric.py     the pure scoring function
  providers/
    claude.py           Claude on Vertex AI / first-party
    replay.py           offline detector for tests and demos
  store/ledger.py       the SQLite ledger: cache, memory, conventions
docs/                   architecture, reproducibility spec, GCP setup
```

## Development

```bash
pytest              # 89 tests
ruff check src tests
ruff format src tests
```

The test suite is the specification: [`tests/test_rubric.py`](tests/test_rubric.py)
asserts the rating is a pure function,
[`tests/test_grounding.py`](tests/test_grounding.py) asserts hallucinations are
discarded, and [`tests/test_detector_contract.py`](tests/test_detector_contract.py)
pins the Claude request shape so a stale API assumption fails in CI rather than
in production.

## Licence

Apache 2.0.
