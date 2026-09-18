# SI-02 — Phased build plan

Target: a digital twin that is a **decision and simulation system**, which is the
literal wording of the key challenge. Everything below is ordered so that if you
run out of time at the end of any phase, what you have is still demoable.

Hours are calibrated to a 36-hour hackathon with 3–4 people. Scale proportionally.

---

## Phase 0 — Decisions to lock before writing UI code (1 h, whole team)

Lock these and do not revisit. Reopening them mid-build is how teams lose Sunday
morning.

| Decision | Choice | Reason |
|---|---|---|
| Simulation paradigm | Discrete-event, hand-rolled | §1 of `ENGINE_SPEC.md` |
| Engine language | Python 3.10+, stdlib only | zero install friction, deploys anywhere |
| Backend | FastAPI + Uvicorn | dicts from the engine are already JSON |
| Frontend | React + Vite, React Flow for the canvas, Recharts for charts | React Flow gives node/edge dragging free |
| State | Baseline plant JSON is the single source of truth; scenarios are patch lists | see §5.1 of the spec |
| LLM | Narration only, never computation | §Phase 6 |
| Demo data | `plants/mdfs.json`, committed | never live-scrape during a demo |
| Replications | 10 by default, 6 in "fast" mode | 10 gives ±2.7 units/h at 95% |

**Rename these in every artefact you produce.** The old names are credibility
liabilities in front of a technical judge:

| Old name | New name | Why |
|---|---|---|
| "JSON Converter ML Model" | Schema-constrained extractor | It is an LLM producing JSON against a schema, plus validation. Not a trained model. |
| "Component Mapper ML Model" | Topology compiler | Deterministic mapping from validated JSON to `Plant`. Pure code. |
| "Simulation Scanner" | Discrete-event simulation kernel | "Scanner" suggests polling a dashboard. |
| "real-time public data" | Industry reference priors (estimated) | See Phase 5. |

---

## Phase 1 — Simulation core ✅ ALREADY BUILT

Delivered in `dtwin/`. Runs standalone, produces real numbers, self-validates.
Read `ENGINE_SPEC.md` once as a team — every one of you must be able to explain
BAS blocking and common random numbers, because that is what you will be asked.

**Remaining work in this phase (2 h):**

1. Write `tests/test_engine.py`. Four tests, which double as proof to judges:
   - A single-stage line with deterministic 60 s processing and no failures
     produces exactly 60 units/h.
   - A two-stage line where stage 2 is half the speed: stage 1's blocked fraction
     converges to 50%.
   - `A = MTBF/(MTBF+MTTR)`: a stage's simulated `DOWN` fraction matches `1−A`
     within 3% over 50 replications.
   - Determinism: same plant + same seed ⇒ byte-identical KPIs.
2. Add `--fast` (6 reps, 4 h horizon) for interactive UI calls.

> **Copilot prompt.** `Write pytest tests in tests/test_engine.py for the dtwin
> package. Test 1: a Plant with one Stage (machines=1, proc det 60s, no failures,
> yield 1.0, horizon 7200, warmup 600) has throughput_per_h within 1% of 60.
> Test 2: two stages, stage1 det 30s, stage2 det 60s, in_buffer 1 — assert
> stage1 blocked fraction is between 0.45 and 0.55. Test 3: one stage with
> mtbf=1000, repair mean 250 — over seeds 1..50 assert mean DOWN fraction is
> within 0.03 of 0.2. Test 4: Simulation(plant, seed=7).run() twice gives
> identical kpis() dicts. Use only the public API in dtwin/.`

---

## Phase 2 — API layer (3 h)

Thin. The engine returns dicts; the API serialises them and manages a scenario
store. No business logic here.

```
POST   /api/plants                 body: plant JSON            -> {plant_id}
GET    /api/plants/{id}                                        -> plant JSON
POST   /api/plants/{id}/run        {reps, fast}                -> full_report + replication CI
POST   /api/plants/{id}/whatif     {patches[], reps}           -> compare() output
POST   /api/plants/{id}/sweep      {levers[], reps}            -> rank_interventions() output
GET    /api/plants/{id}/timeline   {seed}                      -> state intervals for the Gantt
POST   /api/explain                {context_json, question}    -> LLM narration (Phase 6)
```

Rules:
- `/run` and `/whatif` are **synchronous** at 10 reps (≈1 s and ≈2 s). Do not build
  a job queue; you do not have time and you do not need it.
- `/sweep` takes ~17 s. Return a job id and poll, or run it once on plant creation
  and cache. Caching is simpler and is what you will demo.
- Cache key: `sha256(plant_json + patches_json + seeds)`. Scenario results are pure
  functions of their inputs, so the cache never goes stale. This makes the demo
  instant on a second click.
- Enable CORS for the Vite dev server on day one, or you will lose 40 minutes to it
  at 2 a.m.

> **Copilot prompt.** `Create api.py: a FastAPI app exposing the endpoints above.
> Import Plant, Simulation from dtwin, full_report from dtwin.analysis, and
> Patch/compare/rank_interventions/replicate from dtwin.whatif. Store plants in an
> in-memory dict keyed by uuid4. Use Pydantic models for request bodies. Add an
> LRU cache keyed on sha256 of (plant dict, patches, seeds) around the run and
> whatif handlers. Enable CORS for http://localhost:5173. Convert float('inf') to
> null before returning JSON. No other logic — every computation must call into
> the dtwin package.`

---

## Phase 3 — Twin canvas and live analytics (6 h) — **the screen judges score**

A left-to-right node graph, one node per stage, edges are buffers.

**Node rendering — this is where the design work matters.** Each node is a small
stacked bar of its five state fractions in fixed colours:
`BUSY` green, `SETUP` amber, `DOWN` red, `BLOCKED` orange, `STARVED` grey.
A judge should read the line's health in two seconds without a legend: a wall of
grey on the right and orange on the left, with one green node in the middle, *is*
the bottleneck story.

- Bottleneck node: thick border + rank badge. Only the top-ranked stage gets it.
- Edge label: `avg fill / capacity`. Edge thickness ∝ flow rate; edge turns red
  when average fill > 85% (saturated) and dashed when < 20% (oversized).
- Click a node → right panel: capacity `C_i`, the five-way utilisation split,
  failures, scrap, and its row from the propagation table.

**Right column — the anomaly/insight feed.** Render `full_report().inefficiencies`
directly: severity chip, code, stage, and the diagnosis sentence. They are already
sorted by severity. Clicking one highlights the implicated node.

**Loss waterfall.** Five-bar chart from `losses.buckets`. Put it where it is seen
without scrolling; it is the clearest single artefact you have.

**Propagation view.** The `amplification` column as a horizontal bar chart, plus a
"top idle flows" list rendered as `cause → victim`. One sentence of copy above it:
*"each second S3 is down costs the line 3.34 s of idleness elsewhere."*

**Self-validation badge.** Small green "4/4 checks passed" chip in the header,
expandable. Judges notice a system that checks itself.

> **Copilot prompt.** `Build src/components/TwinCanvas.jsx with React Flow. Props:
> report (the /run response). Render one custom node per report.kpis.stages entry
> as a card showing id, name, machine count, and a horizontal 5-segment stacked bar
> of busy/setup/down/blocked/starved with colours #16a34a/#f59e0b/#dc2626/#ea580c/
> #9ca3af. Give the node whose id equals report.bottlenecks[0].id a 3px accent
> border and a "BOTTLENECK" badge. Render edges between consecutive stages labelled
> with buf_avg/buf_cap, red stroke when buf_avg/buf_cap > 0.85, dashed when < 0.2.
> onNodeClick calls props.onSelect(stageId). Layout left-to-right, nodes not
> draggable, fitView on mount.`

---

## Phase 4 — What-if workspace (6 h) — **the key challenge; do not under-build it**

Left: baseline, frozen. Right: editable scenario. Centre: the delta.

1. **Parameter editor.** For the selected stage: cycle time, CV, machines, buffer,
   MTBF, MTTR, yield, setup, batch. Each control emits a `Patch`. Show the patch
   list as chips — `S3.proc.mean ×1.20` — so the user always sees exactly what
   changed. An "×" on each chip reverts that one patch.

2. **Preset buttons.** Judges must never have to type during your demo:
   - `CNC tool wear: +20% cycle time`
   - `CNC breakdown: MTTR ×2`
   - `Invest: +1 CNC machine`
   - `Maintenance blitz: MTTR −50%`
   - `Add 10 buffer slots before CNC`
   - `Speed up packaging 30%` ← the one that proves nothing happens

3. **Delta panel.** Render `compare().deltas` verbatim: baseline, scenario, delta,
   %, 95% CI, and a significance pill. **Grey out insignificant rows and label them
   "within run-to-run noise".** This is your highest-value 20 lines of UI. Every
   competing team will show a bare before/after number; you show a confidence
   interval and an honest "no effect".

4. **Bottleneck migration banner.** When `bottleneck_migrated` is true, a full-width
   banner: `Constraint moved S3 → S2`. Animate the badge moving between nodes on
   the canvas. This is the moment the room understands the tool.

5. **Prescriptive panel.** `rank_interventions()` as a ranked table with ΔTH, CI,
   capex, and units/h per 100k. One-click "apply this scenario" on each row.
   Headline it *"we searched 24 interventions; here are the three that work."*

> **Copilot prompt.** `Build src/components/WhatIfPanel.jsx. State: patches array
> of {stage, field, op, value}. Render preset buttons that append fixed patch sets.
> POST patches to /api/plants/{id}/whatif and render the deltas object as a table:
> metric, baseline, scenario, delta, pct, "[ci_low, ci_high]", and a pill that is
> green "significant" or grey "within noise" based on d.significant. Rows with
> significant === false get 50% opacity. If response.bottleneck_migrated, render a
> full-width banner "Constraint moved {bottleneck_before} → {bottleneck_after}".
> Below, render a table of GET /api/plants/{id}/sweep results sorted by delta_th
> descending with an Apply button per row that sets patches to that row's patch set.`

---

## Phase 5 — Ingestion, demoted and made honest (4 h)

Your original plan spends three of five phases here. It is now one phase of seven,
and it is the phase to cut first if you are behind.

### The load-bearing problem

**A web scraper cannot produce machine-level parameters.** No public source
publishes a company's stage cycle times, MTBF, buffer capacities or scrap rates.
This is competitively sensitive process data that lives in an MES, behind a
firewall. If you demo "we scraped the web and built their digital twin", the first
technically literate judge will ask where MTBF came from and the demo is over.

You do not need to abandon the idea — the onboarding wow-factor is real. You need
to stop claiming it is measured data.

### The fix: a three-tier provenance model

Every parameter carries a `source` tag, rendered in the UI as a coloured dot.

| Tier | Tag | Meaning | Where it comes from |
|---|---|---|---|
| A | `measured` | from the customer's own data | CSV/MES upload, DB endpoint |
| B | `reference` | industry archetype | your curated library of parameterised lines |
| C | `estimated` | LLM-inferred from public context | company profile → nearest archetype + scaling |

Path B/C: the user names a company. The LLM identifies the *industry and process
type* from public information — which genuinely is public — and selects the closest
archetype from a library you ship (injection moulding + assembly, PCB SMT line,
pharma packaging, food processing, automotive stamping). Public signals like plant
count, headcount and stated annual output scale the archetype: number of parallel
machines, shift pattern, line count. The LLM never invents an MTBF; it picks an
archetype whose MTBF you sourced from published OEE benchmarks.

**Say this to judges, verbatim:** *"Public sources give us industry and scale, not
cycle times. So we infer the line archetype from public data and mark every
parameter as estimated. The moment a customer uploads one shift of MES data, the
calibration step replaces estimates with measured values and reports how far off we
were."* That is a stronger answer than claiming you scraped the unscrapeable,
because it shows you know what data actually exists.

### Calibration — this is what earns the words "digital twin"

Ship a `calibrate` endpoint. Given an observed shift (`units produced, observed
cycle times, downtime events`), it:

1. Runs the twin on the same horizon.
2. Reports twin-vs-actual error on throughput, WIP and per-stage utilisation.
3. Fits `t_i` and `MTBF/MTTR` per stage by bisection on the observed values.

A twin with no error-versus-reality metric is a simulation, not a twin. One screen
showing *"twin predicts 92.1 units/h, line produced 89.4, error 3.0%"* does more
for your score than the entire scraping pipeline.

### Keep from the original plan

- Template library with MDFS as the hero card — **keep, it is your demo path.**
- "Create new" → AI generation / upload data fork — **keep.**
- Direct DB endpoint ingestion — **keep, but demo it with a committed SQLite/CSV
  file.** Live external DB connections fail on conference wifi.
- LLM company lookup returning top-3 matches — **keep**, relabel the output as
  "industry archetype match", and cache three example companies as fixtures so the
  demo runs offline.

### Cut or park

- Live web scraping during the demo. Pre-scrape, commit the JSON, and load it from
  disk. If you must show the scraper, show it once, early, outside the timed demo.

> **Copilot prompt.** `Create ingest/archetypes.py with a dict of 5 industry
> archetypes, each a complete Plant JSON matching plants/mdfs.json's schema, plus
> metadata {industry, typical_oee, source_note}. Create ingest/compile.py with
> compile_plant(raw: dict) -> (Plant, provenance: dict[str, str]) that maps a
> validated extraction dict onto a Plant, filling any missing field from the
> matched archetype and recording per-field provenance as "measured" | "reference"
> | "estimated". Create ingest/calibrate.py with calibrate(plant, observed: dict,
> seeds) that runs replicate(), compares throughput/WIP/per-stage utilisation
> against observed, and returns {errors, suggested_patches} where suggested_patches
> adjust proc.mean per stage by bisection to match observed utilisation within 2%.`

---

## Phase 6 — LLM narration, grounded (3 h)

The rule: **the LLM never produces a number.** It receives computed numbers and
writes sentences. Every figure in its output must already exist in the JSON you
passed in.

Three uses, in value order:

1. **Root-cause narration.** Click any inefficiency or bottleneck → pass that
   stage's row from the propagation matrix, its utilisation split, and its top
   three idle flows. Ask for three sentences: what is happening, why, what to try.
2. **Scenario comparison.** Pass the `compare()` output. Ask for a plain-English
   summary that must state whether the change was statistically significant.
3. **Executive summary.** Pass the full report. Two paragraphs a plant manager
   could forward.

Prompt discipline:

```
You are given computed simulation results as JSON. Explain them in plain English
for a plant manager. Rules: use ONLY numbers present in the JSON; never estimate
or invent a figure; if a delta is marked significant=false you must say the change
is within run-to-run noise; maximum 4 sentences; no jargon.
```

Then **validate the output**: regex every number out of the LLM response and assert
each appears in the input JSON. If validation fails, fall back to a templated
sentence. Demo that guard if asked about hallucination — having an answer to that
question is worth more than the narration itself.

Cache narrations by input hash. Ship canned fallbacks for the six preset scenarios
so the demo survives a dead API key.

> **Copilot prompt.** `Create llm/narrate.py with explain(context: dict, mode:
> str) -> str. Modes: "root_cause", "scenario", "executive". Build the prompt from
> the system rule above plus json.dumps(context). After the call, extract all
> numeric literals from the response with a regex and assert each appears in
> json.dumps(context) as a substring (rounded to 1 decimal); if any does not,
> return TEMPLATES[mode].format(**context) instead. LRU-cache on sha256 of
> (mode, context). Read the key from env; if unset, return the template.`

---

## Phase 7 — Demo hardening (3 h) — do not skip this

1. **Offline mode.** An env flag that serves every LLM and ingestion response from
   committed fixtures. Test the whole demo with wifi off.
2. **Precompute.** Run `/sweep` for MDFS at startup and cache it. Nothing in the
   demo should take more than 2 s.
3. **Seed everything.** The demo must produce identical numbers every run. Rehearse
   against the numbers you will say out loud.
4. **Screenshot fallback.** Four PNGs in a folder: canvas, waterfall, migration
   banner, prescriptive ranking. If the laptop dies you still present.
5. **Rehearse the 3-minute script below at least four times.** Time it.

---

## The 3-minute demo script

| t | Screen | Say |
|---|---|---|
| 0:00 | Template library → MDFS | "Six-stage moulding and assembly line. Everything you're about to see is computed by a discrete-event simulation, not a dashboard." |
| 0:20 | Canvas | "Colour is machine state. Grey on the right is starvation, orange on the left is blocking. One stage is green the whole shift — S3 is the constraint, and it holds the momentary bottleneck 100% of the shift." |
| 0:45 | Waterfall | "Nameplate is 131 units an hour. Breakdowns take 19, changeovers 8, scrap 11. We deliver 92." |
| 1:05 | Propagation | "Every second the CNC is down idles 3.3 seconds elsewhere. That's the maintenance case, and it's measured by attributing every idle second to a root cause upstream or downstream." |
| 1:30 | What-if → +1 CNC machine | "Ten paired replications under common random numbers. +27.4 units an hour, confidence interval 25.5 to 29.3." |
| 1:50 | Migration banner | "And the constraint moved to S2. Fixing a bottleneck creates the next one — the twin tells you where it went." |
| 2:10 | What-if → packaging 30% faster | "Here's the one nobody shows you. The system says minus 0.19, confidence interval spans zero, within noise. It refuses to credit an improvement at a non-constraint." |
| 2:30 | Prescriptive ranking | "We swept 24 interventions automatically. Halving CNC repair time buys 7.6% throughput for zero capital. Buffer expansion — the intuitive fix — buys 0.2% and adds 43% cycle time." |
| 2:50 | Validation chip | "Four self-checks: Little's Law, state conservation, attribution closure, capacity bound. If any fail we tell you not to trust the run." |

---

## What to say when a judge asks "what's actually running under the hood"

> A custom discrete-event simulation kernel in Python, no framework. Each stage is
> a set of parallel servers with a lognormal processing-time distribution, a finite
> input buffer, an operation-dependent failure process where the time-to-failure
> clock only advances while the machine is cutting and a breakdown preempts and
> resumes the in-progress unit, plus changeover and scrap. Blocking is
> blocking-after-service, so a full downstream buffer stops the upstream machine —
> that is the mechanism that propagates a stoppage backwards through the line. The
> event calendar is a binary heap, so an eight-hour shift is about thirty thousand
> events rather than twenty-eight million time steps. Bottlenecks come from the
> Roser shifting-bottleneck method: we measure which stage holds the longest
> uninterrupted active period at each instant, which finds constraints that move
> during a shift, and we cross-check it against a closed-form capacity bound
> `n_i·A_i·y_i/τ_i` that the simulation is asserted never to exceed. Every idle
> second gets attributed to a root cause by walking the chain of empty or full
> buffers, which is how we get the downtime amplification factor. What-if runs the
> scenario against the baseline under common random numbers with per-stage RNG
> streams, so changing stage three doesn't perturb stage one's draws, and the
> before/after delta is a paired-t with a 95% confidence interval — a single re-run
> on this line has a four-unit-per-hour standard deviation, so an unpaired diff
> would be reporting noise.

Trim to the first four sentences if they look impatient. Lead with "no framework,
custom kernel" — it signals you built rather than wired.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Team spends Saturday on scraping, ships no what-if UI | **High** | Phase 5 is cut-first. Enforce: no ingestion work until Phase 4 demos end-to-end. |
| Judge asks where MTBF came from | **High** | Provenance tags + the verbatim answer in Phase 5. |
| Live LLM/scraper fails on stage | Medium | Offline fixtures (Phase 7.1). |
| What-if delta looks like noise on stage | Medium | Use the preset scenarios; they are chosen to have large, significant effects — except the one that is deliberately insignificant. |
| React Flow layout fights you | Medium | Fixed positions, `nodesDraggable={false}`. Do not build auto-layout. |
| Sweep is too slow in the demo | Low | Precompute and cache at startup. |
| Someone "improves" the RNG seeding | Low | It is load-bearing for CRN. Comment says so; leave it alone. |

---

## Effort reallocation, before and after

| Area | Your original plan | This plan |
|---|---|---|
| Ingestion / onboarding | ~60% | ~15% |
| Simulation core | ~15% | ~25% (already done) |
| Analytics & diagnosis | ~10% | ~20% |
| What-if + prescriptive | ~10% | ~30% |
| LLM narration | ~5% | ~10% |

The problem statement names five analytical outputs and one key challenge. All six
live in the bottom three rows.
