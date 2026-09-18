# DEMO SCRIPT

Three minutes. Every number below is real and reproduces exactly — seeds are
fixed at `1..10`. Rehearse until you are not reading.

**Before you start**

```bash
python3 run.py --check      # must print 16/0 and 15/0
python3 run.py
```

Then: click **Injection Moulding and Assembly — Line A**, wait for the baseline,
and let the Advisor badge finish loading in the background. Turn the wifi off and
run it once more. Nothing in this demo needs a network.

---

## 0:00 — The claim (20 s)

> "A production line is a chain, and its output is set by one link. The hard part
> isn't finding which link — it's knowing whether the thing you're about to spend
> money on will actually help. This is a digital twin that answers that with a
> confidence interval."

Point at the line. Let them look at it for a beat.

> "Six stages. Every machine is coloured by what it's doing right now: green is
> producing, orange is blocked because the next stage can't take the work, grey
> is waiting because the previous stage hasn't delivered."

Click **Play shift**. Let it run a few seconds.

> "Watch the buffers. They fill up on the left and empty on the right. The line is
> telling you where the constraint is before we compute anything."

---

## 0:30 — The diagnosis (40 s)

> "The twin delivers **92.1 good units an hour**, plus or minus 2.7 at 95% over ten
> runs. The constraint is **S3, the CNC machining cell** — it holds the momentary
> bottleneck for **100% of the shift**."

Point at the waterfall.

> "This is where the capacity goes. Design rate of the slowest stage is 130.9.
> Breakdowns take 18.7. Changeovers take 8.1. Scrap takes 10.8. That leaves a
> structural ceiling of 93.3 — and the line delivers 92.1."
>
> "The first four steps are arithmetic. A spreadsheet can do them. **That last
> step can't be done any other way** — it's variability colliding with finite
> buffers, and it's the whole reason this is a simulation and not a calculator."

Point at the propagation table. **This is the number they will remember.**

> "Downtime propagation. Every idle second on this line gets charged to a root
> cause by walking the chain of empty or full buffers back to whatever actually
> stopped. S3 scores **3.34×** — every second the CNC is broken idles **3.34
> further machine-seconds** somewhere else on the line."
>
> "That's the maintenance budget argument, and no dashboard produces it."

---

## 1:10 — What-if: the investment (45 s)

Open **What-if**. Click **Buy capacity**.

> "One more machine at the constraint. This runs ten paired replications against
> the baseline under common random numbers — same seeds, and per-stage random
> streams, so changing stage 3 doesn't disturb stage 1's draws."

Point at the table.

> "**92.1 to 119.5. Plus 27.4 units an hour, up 29.7%**, 95% interval plus 25.5 to
> plus 29.3. Significant. Cycle time drops 156 seconds."

Then the banner — slow down here.

> "**And the constraint moved. S3 to S2.** Fixing a bottleneck creates the next
> one. That's where the *following* investment goes, and the twin tells you
> before you've spent the money."

---

## 1:55 — The refusal (30 s)

**This is the part no other team will have. Do not rush it.**

Click **Speed up a non-constraint**.

> "Now the same machinery on a change that *shouldn't* work — speeding up a stage
> that isn't the constraint by 30%."

Point at the interval.

> "Minus 0.19 units an hour. Interval **minus 0.52 to plus 0.15** — it spans zero.
> The system says **within noise** and refuses to credit it."
>
> "That matters because a single re-run of this line has a standard deviation of
> 4.4 units an hour. Two identical runs can differ by 12 with nothing changed. Any
> what-if that re-runs once and shows you the difference is reporting randomness
> as insight and **cannot tell the difference**. This one can."

---

## 2:25 — Decision, not dashboard (25 s)

Open **Advisor**.

> "The problem statement says use the twin as a decision system. So it searches
> the intervention space itself — every stage, every lever, paired replications
> for each — and throws away anything whose gain isn't statistically real."

Point at rows 1 and 3.

> "Buying a machine gives the biggest gain: plus 28. But **cutting repair time in
> half gives plus 6.9 units an hour for no capital at all**. And per rupee of
> capital, the 20% faster cycle time is the better buy — 12.6 units an hour per
> hundred thousand, against 11.7."
>
> "Nobody had to guess which knob to turn."

---

## 2:50 — Why it's a twin (25 s)

Open **Calibrate**. Click the calibrate button (or have it pre-run).

> "A simulation becomes a twin when it has an error number against the real line.
> Here's one observed shift. The twin predicted 92.1, the line actually ran 83.6 —
> **10.1% off**. It fits cycle times and repair parameters against the shift, and
> converges to **0.7% in three iterations**. Worst single-stage error goes from 28%
> to 1.9%."

If you have ten more seconds, the top-right chip:

> "And it checks itself — Little's Law, machine-state time conservation, idle-time
> attribution closure, and throughput against the closed-form capacity bound.
> **Four of four.** If the model contradicted itself, it would say so."

---

## The two questions to invite

Ask for them if the Q&A is slow. Both have strong answers.

**"Where did the MTBF come from?"** — Open **Line setup** and point at the
provenance dots.

> "Not from the web. No public source publishes a company's cycle times or
> breakdown rates — that lives in an MES behind a firewall. Green is measured from
> the customer's own data, blue is an industry benchmark from published OEE
> figures, amber is estimated for this line. The twin never claims a number is
> measured when it isn't, and calibration is what turns amber into green."

**"Does it always tell you to buy a machine?"** — Switch to **SMT Electronics —
Line B** and run **Buy capacity**.

> "No. Here it's plus 3.1 — real but small — the constraint **doesn't** move, and
> cycle time gets 19.7% *worse* while WIP rises 23%. This line is well balanced;
> extra capacity just adds queue. Different line, different answer, because it's
> computed."

---

## Backup

If the Advisor is still computing when you get there, talk over it — it caches
and the badge shows progress. If anything else goes wrong, `python3 demo.py`
produces the baseline, five scenarios and the ranking in the terminal in about
20 seconds with no browser at all.

Keep screenshots of: the line at mid-shift, the waterfall, the migration banner,
the within-noise interval, the advisor ranking, and the calibration result.

## Numbers, one page

| | |
|---|---|
| Baseline | 92.11 units/h, sd 4.37, CI95 ±2.71 |
| Constraint | S3 CNC Machining, 100% of shift |
| Waterfall | 130.91 → 112.21 → 104.10 → 93.34 → 92.11 |
| Amplification | S3 = 3.34× |
| +1 machine at S3 | +27.40 (+29.7%), CI [+25.52, +29.28], **S3 → S2** |
| Null lever | −0.19, CI [−0.52, +0.15], within noise |
| Advisor #1 | S3 +1 machine, +28.00, 240k, 11.67 per 100k |
| Advisor #2 | S3 20% faster cycle, +15.12, 120k, **12.60 per 100k** |
| Advisor #3 | S3 MTTR −50%, +6.93, **no capital** |
| Calibration | 83.6 observed · 92.1 before (10.1%) · **84.2 after (0.7%)** |
| Self-validation | 4 / 4 |
| SMT counter-example | +3.09 (+3.3%), no migration, cycle time +19.7% |
| Pharma contested BN | S6 60% / S4 33% of shift |


---

# ADDENDUM — the intake demo (two runs, ~3 minutes)

## A. Live twin from a recorded/generated session (100 s)

1. Open a line from **Reference lines**. Go to the **Live** tab.
2. Press **Start engine replay**. The badge reads `CONNECTED · REPLAY ·
   synthetic replay generated by the engine`. Say the word: *replay*. This is
   the real engine's own timeline played back through the same connector
   interface a broker would use — it is not measured data, and the UI never
   pretends otherwise.
3. Watch the machine grid change state as events land. The event feed shows
   `prev -> new` with a provenance tag on every line.
4. When a stage faults, read the **Downtime propagation** line out loud:
   `S2 DOWN -> S1 BLOCKED (observed) -> S3 STARVED (observed)`. Point at the
   tags. Where the source reported the effect it says *observed*; where the twin
   deduced it from serial topology it says *inferred*. That distinction is the
   product.
5. Press **Connect MDFS**. It reports `UNCONFIGURED` and the reason: the
   interface document and captured session were not supplied, so no topics were
   invented. Say that plainly — it is the most credible thing in the demo.
6. Go to **What-if**, add a machine to the constraint, run it. The bottleneck
   migrates. Return to **Live**: the machine grid is unchanged. The live twin
   and the simulation twin are separate objects; a what-if branches from a deep
   copy and a test enforces it.
7. Back in **Live**, press **Run analysis on current state**. The existing
   `full_report` runs once, on demand, against parameters estimated from the
   observed stream. Never per event.

## B. Create New Factory from a CSV (80 s)

1. From the launch screen press **+ Create New Factory**.
2. Name it, choose **Upload a file**, pick `tests/fixtures/greenfield_line.csv`.
   This file shares nothing with MDFS: columns are `Work Centre`,
   `Process Step`, `Event Ts`, `Op Status`, `Cycle (ms)`, `Good Qty`,
   `Reject Qty`, `Queue Len`; times are in milliseconds; stages are Raw
   Material -> Machining -> Inspection -> Assembly -> Packaging.
3. **Discover schema.** Every column is mapped with a confidence and a reason
   ("'Work Centre' is a known alias of machine"). Milliseconds are converted to
   seconds, ISO timestamps to epoch, `RUN`/`BREAKDOWN` to `BUSY`/`DOWN`. The
   quality panel lists anything it could not do.
4. In the mapping review, note that Machining shows **2 machines** — the two
   lathes were grouped into one stage — and that cycle time is tagged
   `observed` with its sample count while MTTR from a single failure is tagged
   `estimated`, not observed.
5. Edit something (rename a stage, change a machine count) to show the table is
   real, then **Review the twin** and **Launch this twin**.
6. You are now in the ordinary workspace. Run **Diagnose**, **What-if** and
   **Advisor**. Same engine, same confidence intervals, different factory. That
   is the universality claim, and there is a test named after it.

## What not to claim

* Do not call the replay live.
* Do not claim the MQTT socket works; the message path is tested, the socket is
  not.
* Do not claim any topology. Serial with parallel machines per stage, and
  branching data gets flagged.
