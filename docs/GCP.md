# Running Caliper on Google Cloud

Caliper's default backend is **Claude on Vertex AI**. Authentication is Google
Application Default Credentials — there is no Anthropic API key on this path.

## 1. Authenticate

Run these yourself; they are interactive and will prompt in a browser.

```bash
gcloud auth login
```

```bash
gcloud config set project YOUR_PROJECT_ID
```

```bash
gcloud auth application-default login
```

The last command is the one that matters: the Anthropic SDK's `AnthropicVertex`
client reads ADC. Without it every request fails with a credentials error.

## 2. Enable the API and grant access

```bash
gcloud services enable aiplatform.googleapis.com
```

Claude models on Vertex must be enabled for the project in **Vertex AI Model
Garden → Anthropic Claude → Enable**. This is a one-time console step per
project and cannot be done from the CLI.

## 3. Configure

```bash
cp .env.example .env
```

Then set, in `.env` or your shell:

```bash
export CALIPER_BACKEND=vertex
export CALIPER_GCP_PROJECT=YOUR_PROJECT_ID
export CALIPER_GCP_REGION=us-central1
export CALIPER_MODEL=claude-opus-5
export CALIPER_EFFORT=high
```

`CALIPER_GCP_REGION` also accepts `global` (recommended when available for your
model), or a multi-region such as `us`.

## 4. Verify the path end to end

```bash
caliper review examples/decent_service --author you@team.dev --passes 3
```

A successful run prints a token line including cache reads. If
`cache reads 0` persists across passes, the prompt prefix is being invalidated —
see [ARCHITECTURE.md](ARCHITECTURE.md#5-detect--providersclaudepy).

## 5. Deploy to Cloud Run

```bash
./infra/deploy.sh YOUR_PROJECT_ID us-central1
```

The script builds the container, pushes it to Artifact Registry, and deploys a
Cloud Run service with a service account holding `roles/aiplatform.user`. It
deploys with `--no-allow-unauthenticated`; Caliper has no authentication of its
own and must not be exposed publicly.

Call it with an identity token:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     -H "Content-Type: application/json" \
     -d '{"author":"you@team.dev","files":{"a.py":"x = eval(input())\n"}}' \
     "$SERVICE_URL/v1/reviews"
```

## Persistence in Cloud Run

The SQLite ledger lives on the container filesystem, which is ephemeral — a new
revision starts with no history, so the Tier 1 cache and recurrence tracking
reset. For anything beyond a demo, mount a Cloud Storage volume at
`/data` and set `CALIPER_LEDGER=/data/ledger.db`, or port `store/ledger.py` to
Cloud SQL. `Ledger` is the only module that touches SQL and its interface is six
methods.

## Organisation policy can disable features silently

Vertex enforces `constraints/vertexai.allowedPartnerModelFeatures`, which allows
an organisation to permit or deny individual features **per partner model**.
Two of them matter to Caliper, and they fail in different and confusing ways.

### `structured_outputs`

If disabled, both of these are rejected with an HTTP 400:

- `output_config.format` — the structured output path
- `strict: true` on a tool definition — the same feature under another name

Ordinary tool use is still permitted. Caliper detects this specific rejection
and falls back automatically to a forced call to a single `report_findings`
tool with the same schema, then validates each finding individually since the
shape is no longer guaranteed. The fallback is narrow on purpose — any other
400 surfaces rather than being quietly downgraded — and it is sticky, so the
wall is hit once per process rather than once per pass.

The error looks like this:

```
BadRequestError: Error code: 400 - Organization Policy constraint
constraints/vertexai.allowedPartnerModelFeatures violated for `projects/NNN`
attempting to use a disallowed feature structured_outputs for Partner model
claude-opus-4-6. Please contact your organization administrator to fix this
violation by adding `publishers/anthropic/models/claude-opus-4-6:structured_outputs`
to the allowed values.
```

If your project is known to have this restriction, set
`CALIPER_OUTPUT_MODE=tool` to skip the wasted first attempt.

### Prompt caching

This one is worse, because **it does not raise**. `cache_control` is accepted,
the request succeeds, and `cache_creation_input_tokens` and
`cache_read_input_tokens` both come back zero. Nothing is cached and nothing
says so — the only symptom is a bill several times larger than it should be.

Caliper checks for it and reports it:

```
tokens in 8,412 out 1,784 · cache reads 0 (0% of cacheable)
Prompt caching appears inactive — nothing was written to or read from cache
across passes.
```

If you see that on a multi-pass run, ask your administrator to add
`publishers/anthropic/models/<model>:prompt_caching` to the allowed values.

### Checking what your project allows

The quickest test is to try each feature against a trivial request:

```bash
python - <<'PY'
from anthropic import AnthropicVertex
c = AnthropicVertex(project_id="YOUR_PROJECT", region="us-east5")
base = dict(model="claude-opus-5", max_tokens=32,
            messages=[{"role": "user", "content": "Say OK"}])
for label, extra in [
    ("baseline", {}),
    ("thinking", {"thinking": {"type": "adaptive"}}),
    ("effort", {"output_config": {"effort": "medium"}}),
    ("structured", {"output_config": {"format": {"type": "json_schema", "schema":
        {"type": "object", "properties": {"ok": {"type": "boolean"}},
         "required": ["ok"], "additionalProperties": False}}}}),
]:
    try:
        m = c.messages.create(**base, **extra)
        print(f"{label:<12} OK  cache_write={m.usage.cache_creation_input_tokens}")
    except Exception as e:
        print(f"{label:<12} {type(e).__name__}: {str(e)[:120]}")
PY
```

A 404 rather than a 403 means the *model* is not enabled for the project — that
is Model Garden, not org policy. Model availability also varies by region: at
the time of writing, `claude-opus-4-6` answers on `us-east5` where the same
project returns 404 on `us-central1`.

## Feature availability on Vertex

Relevant differences from the first-party API, all accounted for in the code:

| Feature | Vertex | Consequence for Caliper |
|---|---|---|
| Messages, streaming, tool use | ✅ | — |
| Structured outputs / strict schema | ✅ | Detection uses `output_config.format` |
| Adaptive thinking + effort | ✅ | `{"type": "adaptive"}` + `effort` |
| Prompt caching (5m, 1h) | ✅ | Used, with an explicit breakpoint |
| **Automatic** prompt caching | ❌ | `cache_control` placed by hand on the last system block |
| Message Batches | ❌ | Bulk backfill runs as concurrent requests, not a batch job |
| Files API | ❌ | Code is sent inline |

## Costs

A review is `--passes` requests. The system prefix is cached, so passes 2..K read
most of their input from cache at roughly a tenth of the input rate. The
dominant cost is the code itself, sent once per pass. Two levers:

- `--passes 3` instead of 5 — cheaper, wider Tier 2 band.
- `CALIPER_EFFORT=medium` — cheaper, lower detection recall.

Run `caliper verify` after changing either. The point of measuring your own
variance is to make that trade with a number rather than a guess.
