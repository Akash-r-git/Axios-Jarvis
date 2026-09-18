# JUDGE Q&A

Q&A is scored. The pattern that wins it: **answer in one sentence, then give the
mechanism, then volunteer the limitation.** Volunteering the limitation is what
separates a team that understands its system from one that memorised a pitch.

---

## The one-paragraph answer to "what's actually running under the hood"

Say this more or less verbatim. Lead with *no framework* — it signals you built
rather than wired.

> A custom discrete-event simulation kernel in Python, no framework. Each stage
> is a set of parallel servers with a lognormal processing-time distribution, a
> finite input buffer, an operation-dependent failure process where the
> time-to-failure clock only advances while the machine is cutting and a
> breakdown preempts and resumes the in-progress unit, plus changeover and
> scrap. Blocking is blocking-after-service, so a full downstream buffer stops
> the upstream machine — that is the mechanism that propagates a stoppage
> backwards through the line. The event calendar is a binary heap, so an
> eight-hour shift is about thirty thousand events rather than twenty-eight
> million time steps. Bottlenecks come from the Roser shifting-bottleneck
> method: we measure which stage holds the longest uninterrupted active period
> at each instant, which finds constraints that move during a shift, and we
> cross-check it against a closed-form capacity bound that the simulation is
> asserted never to exceed. Every idle second gets attributed to a root cause by
> walking the chain of empty or full buffers, which is how we get the downtime
> amplification factor. What-if runs the scenario against the baseline under
> common random numbers with per-stage RNG streams, so changing stage three
> doesn't perturb stage one's draws, and the before/after delta is a paired-t
> with a 95% confidence interval — a single re-run on this line has a
> four-unit-per-hour standard deviation, so an unpaired diff would be reporting
> noise.

If they look impatient, stop after the fourth sentence.

---

## Modelling choices

**Why discrete-event and not queueing theory?**
Queueing theory gives closed-form answers in milliseconds, but a Jackson network
requires exponential service times, infinite buffers, no failures and no
blocking. A real line violates all four, and finite-buffer lines with blocking
have no general closed form. We didn't discard queueing theory though — we
compute the closed-form capacity bound and assert the simulation never exceeds
it. It's one of our four self-validation checks. So it's a cross-check rather
than the engine.

**Why not agent-based?**
Agent-based modelling is for entities that make autonomous decisions. A
workpiece makes none — it gets pushed. Modelling it as an agent adds a
decision layer that is empty. If we modelled operators choosing which machine to
attend, that would be a genuine case for agents, and it's the natural extension.

**Why not SimPy?**
SimPy gives you a clock and process abstractions. The three things that make
this system work — per-stage RNG streams, root-cause idle attribution, and cheap
model deep-copy for scenario trees — are more code *on top of* SimPy than the
event loop is by itself. And we'd have inherited a dependency for a project
whose selling point is that it runs with zero installs.

**Why lognormal and not exponential processing times?**
The exponential distribution has its mode at zero, which says the most likely
duration of a machining operation is instantaneous. That's physically absurd and
it systematically overstates congestion. Lognormal is right-skewed with a mode
away from zero, which is what a real cycle-time histogram looks like. We
parameterise it as mean and coefficient of variation rather than mean and
variance, because CV is what an MES export actually gives you and it's
scale-free — "20% more variable" is a meaningful edit.

**Why does blocking matter so much?**
It's the only mechanism in the model by which a stoppage travels *upstream*.
We use blocking-after-service: a machine that finishes a unit with no room
downstream keeps holding it and stops. Without that, the model shows starvation
propagating forwards but never back-pressure propagating backwards — and
"downtime propagation", which the problem statement asks for by name, becomes
unanswerable.

**Why operation-dependent failures?**
The time-to-failure clock only advances while the machine is BUSY. A wall-clock
failure model would keep accumulating failure risk while a machine sits starved
and idle, which inflates apparent availability on a starved line and can hand
you the wrong bottleneck entirely. Failures are also preempt-resume: a breakdown
interrupts the in-progress unit and it resumes afterwards rather than restarting.

---

## Statistics

**Why not just re-run once and show the difference?**
Because on this line that difference is mostly noise. Throughput over an
eight-hour shift has a run-to-run standard deviation of 4.4 units/h against a
mean of 92.1 — two runs with *nothing changed* can differ by ±12. A single
re-run can't distinguish a real 5% improvement from randomness. We run ten
paired replications and report a paired-t confidence interval.

**What are common random numbers actually doing?**
Reducing variance by pairing. Baseline and scenario see identical failure
patterns and identical processing-time draws, so the difference between them
isolates the change instead of mixing in the difference between two random
worlds. The interval gets much tighter for the same compute.

**Why per-stage streams and not one stream?**
With one shared stream, editing stage 3 changes how many draws are consumed
before stage 1's next draw, which reshuffles the entire line's randomness and
destroys the pairing. Streams are keyed on `(seed, stage_id, purpose)` so stage 1
draws from its own stream regardless of what you do to stage 3.

**Why CRC32 rather than Python's `hash()`?**
Python salts string hashing per process, so the same seed would give different
results after a restart. CRC32 is stable, which is what makes the demo numbers
reproducible.

**Is ten replications enough?**
Enough to decide the questions we're asking — the interval on the +1 machine
scenario is +25.5 to +29.3, nowhere near zero. It's *not* enough for a marginal
change, and the honest answer is that the system tells you: if the interval spans
zero, either the effect is null or you need more replications, and the fix is to
raise the replication count. Ten is a demo-latency choice, not a statistical
claim.

**Is the paired-t valid here? Throughput isn't normal.**
The test is on the *paired differences*, not the raw values, and with common
random numbers those differences are far better behaved than either series. At
n=10 we're leaning on the CLT and it's the standard practice in simulation
output analysis. For a production system we'd add a bootstrap interval as a
non-parametric cross-check.

---

## Data and honesty

**Where did the MTBF come from?** *(Expect this. It ends unprepared demos.)*
Not from the web. No public source publishes a company's stage cycle times,
MTBF, buffer capacities or scrap rates — that lives in an MES behind a firewall
and it's competitively sensitive. So we don't pretend to scrape it. Every
parameter carries a provenance tier shown as a coloured dot: **measured** from
the customer's own data, **benchmark** from our archetype library sourced from
published OEE figures, or **estimated** for this line. What is genuinely public
is the industry and the process type, so the LLM identifies those and selects
the nearest archetype — it never invents an MTBF. Calibration against one
observed shift is what turns amber into green.

**Then how is this a digital twin rather than a simulation?**
Because it has a measured error against the real line. Give it one observed
shift and it reports twin-versus-actual error on throughput and per-stage
utilisation: 10.1% before fitting, 0.7% after, worst single-stage error from 28%
down to 1.9%, in three iterations. A simulation that has never been compared to
reality isn't a twin.

**What were the "ML models" in your original plan?**
They were mislabelled and we renamed them. The JSON converter is an LLM doing
schema-constrained extraction; the component mapper is a deterministic compiler.
Neither is trained, neither has labels, and calling them ML models would have
invited exactly that question with no good answer.

**How do you know the simulation is right?**
Two ways. First, sixteen known-answer tests — not consistency checks. A
deterministic single stage must produce exactly 60 units/h. Blocked fraction
must converge to 50% when the downstream stage is twice as slow. Downtime
fraction must match MTBF/(MTBF+MTTR). Changeover must cost exactly setup over
batch per unit. Second, four self-validation checks run on every simulation and
are shown in the interface: Little's Law, machine-state time conservation,
idle-time attribution closure, and throughput against the closed-form capacity
bound. If the model contradicted itself, the interface would say so rather than
quietly reporting a number.

---

## Hard ones

**Your bottleneck is just the busiest machine, isn't it?**
No, and that distinction is the point. Utilisation ranking gives one static
answer per shift. We use the Roser shifting-bottleneck method: the constraint at
any instant is the stage holding the *longest uninterrupted active period*,
because everything else has been interrupted waiting on it. It has no tuning
threshold and it finds constraints that **move during a shift**. Our
pharmaceutical line is the demonstration — S6 holds the bottleneck for 60% of
the shift and S4 for 33%. A utilisation ranking cannot express that at all. And
we cross-check against the static capacity ranking; on the moulding line both
methods independently name S3, which is the reassuring case.

**What if the two bottleneck methods disagree?**
Then the line has a moving constraint and the static model is the one that's
wrong — it assumes a steady state that doesn't exist. We show both, and
disagreement is information rather than a bug. It's the signal that buffer
placement, not stage capacity, is the binding problem.

**Buffers fix bottlenecks. Why does your tool say otherwise?**
Because they don't, and the tool measures it. On the moulding line, adding five
buffer slots is one of the levers the advisor *drops* — the gain isn't
statistically distinguishable from zero, while cycle time and WIP both rise.
Buffers decouple variability; they don't add capacity. If the constraint is
capacity-bound, a buffer converts a stoppage into a queue. That contrast is one
of the more useful things the system teaches.

**Does it always recommend buying a machine?**
No. Run the SMT line: +1 machine at the constraint gives +3.1 units/h — real but
small — the constraint doesn't move, and cycle time gets 19.7% *worse* while WIP
rises 23%. That line is well balanced, so extra capacity just adds queue.
Different line, different answer, because it's computed rather than written.

**Thresholds — aren't those arbitrary?**
The eight inefficiency detectors do fire on hand-picked cut-offs, and those are
the only tuned constants in the whole system. We'd own that. Bottleneck
detection, propagation attribution and the what-if statistics have no tunable
thresholds at all — which is deliberate, because those are the outputs someone
would act on.

**What breaks at scale?**
The event kernel is O(events log events) and a shift is around thirty thousand
events, so a much longer horizon or many more stages is fine. What doesn't scale
is the advisor: it's stages × levers × replications simulations. On six stages
that's fast; on sixty it needs either parallel replications or ranking candidates
by the static capacity model before simulating them. That's the next engineering
problem, and it's a known one.

**What's missing?**
Four things, honestly. Serial topology only — no parallel routing, re-entrant
flow, assembly trees or rework loops; the kernel would extend but the
buffer-chain walk in the analysis layer assumes a single path. No labour or
tooling model, so a line constrained by operators rather than machines is out of
scope. Calibration fits cycle times and MTBF/MTTR only — it can't infer buffer
capacities or topology. And sessions are in-memory, which was the price of
having zero dependencies.

**Calibration sounds too good. What's the catch?**
There's a real identifiability limit and we found it the hard way. Busy fraction
is invariant under uniform scaling of all processing times — scale every cycle
time by the same factor and throughput scales inversely, so utilisation cancels
exactly. Our first fit reported worst-stage error of 0.5% while every parameter
was 6.4% wrong. Utilisation can only fix the *ratios* between stages; throughput
is needed to pin the absolute time scale. The fit alternates the two, which is
why it converges properly now.

**Why not build on the existing open-source takt simulator?**
We read it. It runs a time-stepped deterministic tick loop with threshold rules
and no stochastic model at all. With no randomness there's nothing to replicate,
so paired comparison and confidence intervals are impossible, and there's no
mechanism for root-cause attribution to attribute *to*. It's also hard-coded to
one localised factory taxonomy. We took UI ideas from it and wrote our own
engine.

---

## If you're asked something you don't know

Say so, then say what you'd do. *"I don't know — here's how I'd find out"* scores
better than a confident wrong answer, and a judge who knows manufacturing will
detect the difference immediately. The system's own posture is to refuse to
credit what it can't measure; match it.
