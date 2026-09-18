# Maestro Twin

**A production-line digital twin that tells you what to fix, and how sure it is.**

Built for SI-02 — *Production Line Digital Twin & Bottleneck Intelligence*.

A multi-stage manufacturing line is simulated event by event. The twin finds the
constraint, measures how far one machine's downtime spreads through the rest of
the line, decomposes throughput loss into its causes, and answers *"what happens
if…"* with a confidence interval rather than a single re-run. It then searches
the intervention space itself and ranks every available change by throughput
gained per unit of capital.

**Python 3.10+. No pip install. No npm install. No network. One command.**

```bash
python3 run.py
```

Opens on `http://127.0.0.1:8000`. Nothing is sent anywhere; the whole thing runs
on localhost.

```bash
python3 run.py --check          # 16 engine tests + 15 interface tests
python3 run.py --port 9000      # different port
python3 run.py --no-browser     # serve without opening a browser
python3 demo.py                 # terminal-only: baseline + scenarios + ranking
```

---

## What it does

**Diagnose.** The line renders as machines and buffers, each machine coloured by
state — producing, in changeover, broken down, blocked by the next stage, waiting
on the previous one. Scrub through the shift and watch buffers fill upstream of
the constraint and empty downstream of it. Alongside: the throughput-loss
waterfall, the downtime-propagation table, and a severity-ranked feed of
inefficiencies, each with a diagnosis rather than a flag.

**What-if.** Change any parameter on any stage, or click a preset. The scenario
runs against the baseline under *common random numbers* — the same seeds, with
per-stage random streams, so changing stage 3 does not disturb stage 1's draws.
The result is a paired comparison with a 95% confidence interval. When a change
cannot be distinguished from run-to-run noise, it is labelled **within noise**
and gets no credit. When fixing one bottleneck creates the next, a banner says
so and names it.

**Advisor.** Every stage is perturbed on every lever — faster cycle, extra
machine, shorter repair, more buffer — with paired replications for each. Levers
whose gain is not statistically real are dropped. The survivors are ranked by
throughput gain and by units/h per 100k of capital. Nobody has to guess which
knob to turn.

**Calibrate.** Give it one observed shift (`data/observed_shift.csv`) and it
reports twin-versus-actual error on throughput and per-stage utilisation, then
fits cycle times and MTBF/MTTR to close the gap. On the reference line: 10.1%
throughput error before, **0.7% after**, worst per-stage error 28.0% → 1.9%, in
three iterations. This is what earns the phrase *digital twin* rather than
*simulation*.

**Line setup.** Every parameter, with a coloured dot for where it came from:
measured from your data, industry benchmark, or estimated for this line. The
twin never claims a number is measured when it is not.

---

## Why these methods

Fuller treatment in `ENGINE_SPEC.md`. In short:

**Discrete-event simulation, hand-rolled, no framework.** Queueing theory gives
closed-form answers in milliseconds but needs exponential service times,
infinite buffers, no failures and no blocking — a real line violates all four,
and finite-buffer lines with blocking have no general closed form. Agent-based
modelling is for entities that make autonomous decisions; a workpiece makes
none, it is pushed. DES represents exactly the mechanisms the problem statement
names. Queueing theory still earns a place as a **cross-check**: the closed-form
capacity bound is computed and the simulation is asserted never to exceed it.

Three mechanics carry the weight:

- **Blocking after service.** A machine that finishes with no room downstream
  keeps holding the unit and stops. This is the *only* mechanism by which a
  stoppage travels upstream. Without it the model shows starvation but never
  back-pressure, and "downtime propagation" is unanswerable.
- **Operation-dependent failures, preempt-resume.** The time-to-failure clock
  advances only while a machine is BUSY. Wall-clock failure models inflate
  availability on a starved line and hand you the wrong bottleneck.
- **Five exhaustive machine states**, whose durations are asserted to sum to the
  observation window.

**Bottlenecks by two independent methods.** A static closed-form capacity
ranking, and the Roser shifting-bottleneck method — the constraint is the stage
holding the longest uninterrupted active period at each instant, because
everything else is waiting on it. No tuning threshold, and it finds constraints
that *move during a shift*, which a utilisation ranking structurally cannot. The
pharmaceutical reference line has a genuinely contested bottleneck (S6 at 60% of
the shift, S4 at 33%) and exists to demonstrate exactly that.

**Downtime propagation by attribution, not correlation.** Every idle second is
charged to a root cause by walking the chain of empty or full buffers until it
reaches a machine that is down, in changeover, or simply too slow. Aggregating
gives an **amplification factor**: on the reference line, S3 scores **3.34×** —
each second the CNC is broken idles 3.34 further machine-seconds elsewhere. That
single number is the maintenance-budget argument, and no dashboard produces it.

**Throughput loss as a waterfall**, each step a distinct mechanism:

```
nameplate rate of slowest stage       130.91 units/h
after breakdowns                      112.21   −18.70
after changeovers                     104.10   − 8.11
after scrap                            93.34   −10.75
actually delivered                     92.11   − 1.23   ← flow loss
```

The first four steps are arithmetic a spreadsheet can do. The last one cannot be
computed any other way — it is variability colliding with finite buffers, and it
is the entire justification for simulating rather than calculating.

**Statistics, because a single re-run is noise.** Throughput on this line has a
run-to-run standard deviation of 4.4 units/h against a mean of 92.1. Two
identical runs can differ by ±12 with nothing changed. Any what-if feature
without replications and paired comparison is reporting randomness as insight
and cannot tell the difference.

---

## Testing

```
$ python3 run.py --check
  16 passed, 0 failed      engine     (tests/test_engine.py)
  16 passed, 0 failed      live       (tests/test_live.py)
  11 passed, 0 failed      ingestion  (tests/test_ingest.py)
  15 passed, 0 failed      interface  (tests/test_ui.js)
```

The engine tests are **known-answer** tests, not consistency checks. A
deterministic single stage must produce exactly 60 units/h. Blocked fraction
must converge to 50% when the downstream stage is 2× slower. Simulated downtime
must match MTBF/(MTBF+MTTR). Changeover must cost exactly setup/batch per unit.
Editing one stage must leave another stage's random draws bit-identical.
Replication-mean throughput must stay under the closed-form capacity bound.

The live and ingestion suites cover the intake layer: event application and
recovery, malformed events flagged rather than applied, bounded history,
connector failure with backoff and last-known-state retention, MDFS message
parsing through an injected profile, what-if isolation, alias/unit/timestamp/
state normalization, every data-quality warning, mapper confidence and edits,
non-serial data flagged, anomaly detection (including a clean stream producing
zero anomalies), Gemini output rejection, and the universality test: a
non-MDFS CSV fixture goes normalize -> map -> Plant -> `full_report` plus a
what-if comparison on the unchanged engine.

Node is used only for the interface tests. It is not needed to run the app —
`--check` reports `SKIP` if it is absent.

---

## Layout

```
run.py                    one command: preflight, serve, open
demo.py                   terminal-only run, no browser

dtwin/model.py            Stage/Plant data model, distributions, capacity maths
dtwin/engine.py           DES kernel: blocking, preempt-resume failures,
                          idle-time attribution, playback frames
dtwin/analysis.py         KPIs, bottleneck ranking (static + Roser), propagation
                          matrix, loss waterfall, inefficiency detectors,
                          self-validation
dtwin/whatif.py           patches, CRN replication, paired-t, prescriptive sweeps
dtwin/calibrate.py        twin-vs-actual fidelity, parameter fitting
dtwin/archetypes.py       industry archetype library and matching
dtwin/narrate.py          plain-English narration, guarded against invented numbers
dtwin/report.py           plain-text rendering

server/api.py             stdlib HTTP server, JSON API, session cache
server/static/            index.html · app.js · styles.css   (no build step)

plants/mdfs.json              injection moulding and assembly — Line A
plants/pharma_packaging.json  pharmaceutical vial packaging — Line C
plants/smt_line.json          SMT electronics assembly — Line B
data/observed_shift.csv       one observed shift, for calibration

tests/test_engine.py      16 engine tests
tests/test_ui.js          15 interface tests
```

Docs: `ENGINE_SPEC.md` (the mathematics and the limitations),
`TASKMANAGER.md` (every change, why, and how it was proved),
`DEMO_SCRIPT.md` (timed walkthrough with the exact numbers),
`JUDGE_QA.md` (the questions and the answers).

---

## Use it as a library

`full_report` and `compare` return plain JSON-serialisable dicts.

```python
from dtwin import Plant, Simulation
from dtwin.analysis import full_report
from dtwin.whatif import Patch, compare, rank_interventions

plant  = Plant.load("plants/mdfs.json")
seeds  = list(range(1, 11))

report = full_report(Simulation(plant, seed=1).run())
cmp    = compare(plant, [Patch("S3", "machines", "add", 1)], seeds)
ranked = rank_interventions(plant, seeds)

print(cmp["deltas"]["throughput_per_h"])
# {'delta': 27.40, 'ci_low': 25.52, 'ci_high': 29.28, 'significant': True, ...}
```

## HTTP API

```
GET  /api/catalog                        available lines and observation files
POST /api/session                        open a session on a line
GET  /api/session/{id}/baseline?reps=10  report, replications, playback frames
POST /api/session/{id}/whatif            {patches, reps} → paired deltas + CIs
GET  /api/session/{id}/advisor?reps=6    ranked interventions (async job)
POST /api/session/{id}/calibrate         fit against an observed shift
POST /api/session/{id}/apply             promote a scenario to the new baseline
POST /api/archetype/match                match a company description
```

## Reproducibility

Seeds are `1..10` by default and RNG streams are keyed on
`(seed, stage_id, purpose)` through CRC32 — not Python's `hash()`, which is
salted per process and would break reproducibility across restarts. Every number
in `DEMO_SCRIPT.md` reproduces exactly, in any process, offline.


## Live intake layer (data in, twin out)

The product story is: **connect or upload a factory's data -> it is normalized
and mapped into the canonical model -> a live twin tracks state and event
history -> the same engine runs simulation, analytics and what-if on it.**

The engine did not change to make that true. `dtwin/engine.py`,
`dtwin/analysis.py` and `dtwin/whatif.py` contain no source-specific logic and
import nothing from the intake layer; two tests enforce that boundary by
reading their imports and their text.

```
connector  ->  Event  ->  LiveTwin        (observed state + bounded ledger)
file       ->  normalize -> map -> estimate -> Plant  ->  existing engine
```

### What each connector really does

| Connector | Transport | Status |
|---|---|---|
| `ReplayConnector` | none — plays a recorded or generated session | Works. Everything it emits is tagged `replay` and labelled **REPLAY** in the UI and the API. |
| `FileConnector` | reads a canonical event file, size-capped | Works. |
| `RESTConnector` | polls a JSON endpoint on a configurable interval | Works, tested with an injected opener. Unmapped vendor states are flagged, never coerced. |
| `SQLConnector` | stdlib `sqlite3`, opened read-only (`mode=ro`) | Works. One `SELECT` only; a validator rejects semicolons, comments and every write verb. Other drivers are not implemented and are not shown in the UI. |
| `MDFSConnector` | MQTT first (paho-mqtt, lazy import), REST polling for buffers/downtime/equipment master | **Unconfigured in this repository.** See below. |

### The MDFS connector, honestly

`docs/mdfs_interface.md` and `tests/fixtures/mdfs_capture/` were **not supplied
with this repository**. Without them there are no real topics, REST paths or
field names, and inventing them is exactly the fake-integration failure this
project refuses. So:

* the connector ships with **no profile**, reports `UNCONFIGURED` with the
  reason, and the UI shows that reason verbatim;
* the message-handling, state-mapping, reconnect and backoff logic is complete
  and generic over an injected `MDFSProfile`, and is tested against
  MDFS-shaped messages through a fake MQTT client;
* pressing **Connect MDFS** falls back to the engine-generated replay and says
  so;
* **the live socket path is unverified until it is run against a real MDFS
  instance.** Nothing here proves a broker connection works.

Supplying the interface doc means writing one `MDFSProfile` (topic + field
names) and setting `MDFS_MQTT_HOST`; no other code changes.

### State mapping

One explicit table, `MDFS_STATE_MAP` in `dtwin/live/events.py`:

```
RUNNING -> BUSY    FAULTED -> DOWN      CHANGEOVER -> SETUP
BLOCKED -> BLOCKED STARVED -> STARVED   IDLE       -> IDLE
```

`BLOCKED` and `STARVED` reported by a source are **observed**, not inferred, and
are recorded as such. `IDLE` is deliberately **not** folded into `STARVED`:
starvation is a claim about missing material and feeds the loss waterfall, and
a machine that is simply not scheduled makes no such claim. `IDLE` therefore
has no engine counterpart and contributes no loss attribution. Unrecognised
states are counted and surfaced, never guessed.

### Labels you can trust

* **REPLAY** — a recorded or engine-generated session played through the normal
  connector interface. The badge derives this from the connector class, so no
  label can claim "live" for a replay.
* **ESTIMATED** — generated from a description or from too few samples. Never
  called measured.
* Provenance tiers: `observed` / `estimated` / `reference` / `calibrated`
  (`measured` is retained as the original name of the observed tier).

### Serial topology only

The engine models a **serial** line with parallel machines per stage. The mapper
emits exactly that. Data implying branching or merging (a routing column, or
per-job routes that fan out or merge) is **flagged** with the offending stages
and a verdict that admits the limitation — it is never flattened into a fake
series. The twin does not handle arbitrary topologies and does not claim to.

### Adding a connector

1. Subclass `Connector` in `dtwin/live/connectors.py`.
2. Implement `_open`, `_close` and `_read(now) -> Iterable[Event]`. Emit
   canonical `Event`s only; map vendor vocabulary with `map_vendor_state` and
   let unknown values return `None` so they are flagged.
3. Set `delivery` to `live`, `poll` or `replay` — that is what the badge shows.
4. Register it in `server/live_api.start`, and add it to the Data Sources view
   only once it is actually testable. No stub drivers in the UI.

Failures, backoff and last-known-state retention come from the base class.

### Environment variables

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Enables Gemini. Absent: mapping uses the alias tables, description falls back to the archetype matcher, narration uses templates. |
| `GEMINI_MODEL` | Overrides the default model (`gemini-2.5-flash`). |
| `MDFS_MQTT_HOST` / `MDFS_MQTT_PORT` | MDFS broker. Without a profile the connector stays unconfigured regardless. |
| `MDFS_MQTT_USER` / `MDFS_MQTT_PASSWORD` | Optional broker credentials. |
| `MDFS_REST_BASE` / `MDFS_REST_INTERVAL` | MES REST base URL and poll interval. |
| `MAESTRO_VERBOSE` | HTTP request logging. |

Secrets are read from the environment only. They are never returned by the API,
never sent to the browser and never written to disk. `/api/llm` reports whether
a key exists, never what it is.

### What the LLM is allowed to do

Gemini may suggest a canonical target for an ambiguous column, draft an
ESTIMATED factory from a plain-English description, and phrase narration. It
performs no arithmetic, no simulation, no statistics, no bottleneck detection
and no event interpretation. Every response is validated deterministically and
can be rejected: bad mapping targets are dropped with a reason, generated
plants are validated against the schema and repaired values are reported, and
narration containing figures absent from the data is discarded in favour of the
template.

### Live updates are O(1)

Applying an event touches one machine record, one bounded deque and a counter.
**No simulation is ever run per event.** Simulations run only on explicit user
action ("Run analysis on current state", what-if, advisor). The live twin and
the simulation twin are separate objects; every what-if branches from a deep
copy, and a test proves a 3-machine what-if leaves a 2-machine live stage
untouched.


## Known limitations

Serial topology only — branching and merging data is flagged, not modelled;
the MDFS live-socket path is unverified until run against a real instance (its
interface doc and capture were not supplied); XLSX parsing is a small stdlib
reader that refuses large or exotic workbooks; no labour or tooling model; inefficiency thresholds are
the only tuned constants in the system; calibration fits cycle times and
MTBF/MTTR only and has a documented identifiability limit; sessions are
in-memory. Each of these is a deliberate scope decision — see `TASKMANAGER.md §6`
and `ENGINE_SPEC.md` for the reasoning.
