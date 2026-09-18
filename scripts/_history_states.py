#!/usr/bin/env python3
"""
Reconstruct earlier states of server/static/app.js.

git_history.sh replays the build as a series of commits. A fix commit is only
meaningful if it produces a real diff, which means the tree has to be rewound
to the state before that fix. This script does the rewinding by reverse-
applying the known edits, so no stale copies of app.js need to live in the
repository.

    python3 scripts/_history_states.py pre-fixes   # before any defect fix
    python3 scripts/_history_states.py pre-preset  # after D1-D5, before D6
    python3 scripts/_history_states.py final       # restore the shipped file

The shipped file is the source of truth; `final` restores it from a backup
taken on the first call. Every replacement asserts it matched, so a drifted
file fails loudly instead of writing a wrong history.
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "server", "static", "app.js")
# The backup must live outside any directory a commit stages, or it would be
# committed with the interface and deleted again a commit later.
BAK = os.path.join(HERE, "..", ".app.js.final")


# --------------------------------------------------------------------------
# D3 + the n1/n2/n0/pct rewrite: one missing() predicate for all formatters
# --------------------------------------------------------------------------
FORMATTERS_NOW = """/* A missing number must never render as a number. The engine sends null for
   Infinity and NaN (neither is legal JSON), and a metric whose baseline mean
   is zero has an undefined percentage. Without the guard, `null >= 0` is true
   and Number(null) is 0, so a missing value would print as a confident
   "+0.00" — a wrong number shown to a judge is worse than a blank. */
const missing = (x) => x === null || x === undefined ||
                       (typeof x === 'number' && !Number.isFinite(x)) ||
                       Number.isNaN(Number(x));
const n1 = (x) => missing(x) ? '—' : Number(x).toFixed(1);
const n2 = (x) => missing(x) ? '—' : Number(x).toFixed(2);
const n0 = (x) => missing(x) ? '—' : Math.round(Number(x)).toLocaleString();
const pct = (x, d = 0) => missing(x) ? '—' : (Number(x) * 100).toFixed(d) + '%';
const sgn = (x, d = 2) => missing(x) ? '—' : (x >= 0 ? '+' : '') + Number(x).toFixed(d);
const money = (x) => missing(x) || Number(x) === 0
  ? 'none' : Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 });"""

FORMATTERS_WAS = """const n1 = (x) => (x === null || x === undefined || Number.isNaN(x)) ? '—' : Number(x).toFixed(1);
const n2 = (x) => (x === null || x === undefined || Number.isNaN(x)) ? '—' : Number(x).toFixed(2);
const n0 = (x) => (x === null || x === undefined || Number.isNaN(x)) ? '—' : Math.round(Number(x)).toLocaleString();
const pct = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x)) ? '—' : (Number(x) * 100).toFixed(d) + '%';
const sgn = (x, d = 2) => (x >= 0 ? '+' : '') + Number(x).toFixed(d);
const money = (x) => !x ? 'none' : x.toLocaleString(undefined, { maximumFractionDigits: 0 });"""


# --------------------------------------------------------------------------
# D1 + D4: the field table read the Python attribute name off a JSON object,
# and checked only for null when testing for an unlimited buffer
# --------------------------------------------------------------------------
FIELDS_NOW = """/* `f` is the patch field name the engine accepts (dtwin/whatif.py PATCHABLE);
   `get` reads the JSON wire name, which is not always the same string. The
   yield row is the one that differs: the Python attribute is `yield_rate`
   because `yield` is a reserved word, but to_dict() emits the plain `yield`.
   Reading the wrong one returns undefined, and an undefined here used to take
   the whole What-if tab down with it — see TASKMANAGER.md, defect D1. */
const UNLIMITED = (v) => v === null || v === undefined || v < 0;
const FIELDS = [
  { f: 'proc.mean', label: 'Cycle time', unit: 's', min: 2, max: 400, step: 0.5, get: s => s.proc.mean },
  { f: 'proc.cv', label: 'Variability (CV)', unit: '', min: 0, max: 1, step: 0.01, get: s => s.proc.cv },
  { f: 'machines', label: 'Machines', unit: '', min: 1, max: 12, step: 1, get: s => s.machines },
  { f: 'in_buffer', label: 'Input buffer', unit: 'slots', min: 0, max: 60, step: 1,
    get: s => UNLIMITED(s.in_buffer) ? 0 : s.in_buffer, unlimitedAt0: true },
  { f: 'mtbf', label: 'Mean time between failures', unit: 's', min: 600, max: 86400, step: 300, get: s => s.mtbf === null ? 86400 : s.mtbf },
  { f: 'repair.mean', label: 'Repair time', unit: 's', min: 0, max: 3600, step: 30, get: s => s.repair.mean },
  { f: 'yield_rate', label: 'Yield', unit: '', min: 0.5, max: 1, step: 0.005, get: s => s.yield ?? s.yield_rate }
];"""

FIELDS_WAS = """const FIELDS = [
  { f: 'proc.mean', label: 'Cycle time', unit: 's', min: 2, max: 400, step: 0.5, get: s => s.proc.mean },
  { f: 'proc.cv', label: 'Variability (CV)', unit: '', min: 0, max: 1, step: 0.01, get: s => s.proc.cv },
  { f: 'machines', label: 'Machines', unit: '', min: 1, max: 12, step: 1, get: s => s.machines },
  { f: 'in_buffer', label: 'Input buffer', unit: 'slots', min: 0, max: 60, step: 1, get: s => s.in_buffer === null ? 0 : s.in_buffer },
  { f: 'mtbf', label: 'Mean time between failures', unit: 's', min: 600, max: 86400, step: 300, get: s => s.mtbf === null ? 86400 : s.mtbf },
  { f: 'repair.mean', label: 'Repair time', unit: 's', min: 0, max: 3600, step: 30, get: s => s.repair.mean },
  { f: 'yield_rate', label: 'Yield', unit: '', min: 0.5, max: 1, step: 0.005, get: s => s.yield_rate }
];"""


# --------------------------------------------------------------------------
# D2 + D5: fmtField threw on a missing value instead of degrading, and the
# slider preview did not mirror the engine's unlimited-buffer arithmetic
# --------------------------------------------------------------------------
FMT_NOW = """function patchedValue(cur, p) {
  /* Mirrors dtwin/whatif.py apply_patches: an unlimited buffer counts as 0 for
     `mul` and `add`, so the slider preview agrees with what the engine did. */
  const base = missing(cur) ? 0 : cur;
  return p.op === 'set' ? p.value : p.op === 'mul' ? base * p.value : base + p.value;
}
const clampNum = (v, a, b) => Math.max(a, Math.min(b, v));
const fmtField = (F, v) => {
  if (missing(v)) return '—';
  if (F.unlimitedAt0 && v === 0) return 'unlimited';
  return (F.step >= 1 ? String(Math.round(v))
                      : Number(v).toFixed(F.step < 0.05 ? 3 : 2))
         + (F.unit ? ' ' + F.unit : '');
};"""

FMT_WAS = """function patchedValue(cur, p) {
  return p.op === 'set' ? p.value : p.op === 'mul' ? cur * p.value : cur + p.value;
}
const clampNum = (v, a, b) => Math.max(a, Math.min(b, v));
const fmtField = (F, v) => (F.step >= 1 ? String(Math.round(v)) : v.toFixed(F.step < 0.05 ? 3 : 2)) + (F.unit ? ' ' + F.unit : '');"""


# --------------------------------------------------------------------------
# D6: the null-effect preset hard-coded the last stage, which is the
# constraint on the pharmaceutical line
# --------------------------------------------------------------------------
PRESET_NOW = """/* The last preset is the one that should come back "within noise": speeding up
   a stage that is not the constraint cannot move the line. Picking the LAST
   stage is wrong on lines where the last stage IS the constraint — on the
   pharma line that made the "null" lever return a significant +7.3 units/h.
   Pick the stage with the most spare good-unit capacity instead; that one has
   guaranteed slack whatever the topology. */
function slackestStage(bn) {
  const cap = S.baseline.report.losses.capacity_table.filter(c => c.id !== bn);
  if (!cap.length) return bn;
  return cap.reduce((a, c) => (c.r_good > a.r_good ? c : a)).id;
}

const PRESETS = ["""

PRESET_WAS = """const PRESETS = ["""

PRESET_LINE_NOW = """  { t: 'Speed up a non-constraint', d: 'the change that should do nothing', build: bn => [{ stage: slackestStage(bn), field: 'proc.mean', op: 'mul', value: 1 / 1.3 }] }"""

PRESET_LINE_WAS = """  { t: 'Speed up the last stage', d: 'the change that should do nothing', build: () => [{ stage: S.plant.stages[S.plant.stages.length - 1].id, field: 'proc.mean', op: 'mul', value: 1 / 1.3 }] }"""


# --------------------------------------------------------------------------
# The test seam, added with the interface tests
# --------------------------------------------------------------------------
EXPORTS_NOW = """/* exported for the node-based unit tests (tests/test_ui.js) */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { decodeFrames, patchedValue, describePatch, deltaClass,
                     shortBucket, clip, hhmm, pct, sgn, leverPatches,
                     missing, money, n1, n2, fmtField, FIELDS, UNLIMITED,
                     PRESETS, slackestStage, __state: S };
}"""

EXPORTS_WAS = """/* exported for the node-based unit tests */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { decodeFrames, patchedValue, describePatch, deltaClass,
                     shortBucket, clip, hhmm, pct, sgn, leverPatches };
}"""


def rewind(text: str, pairs: list[tuple[str, str]], label: str) -> str:
    for now, was in pairs:
        if text.count(now) != 1:
            raise SystemExit(
                f"error: rewinding to '{label}' failed.\n"
                f"       Expected exactly one occurrence of:\n\n"
                f"{now[:200]}...\n\n"
                f"       found {text.count(now)}. server/static/app.js has "
                f"drifted from what this script knows about.\n"
                f"       Fix the snippet in scripts/_history_states.py rather "
                f"than committing a wrong history.")
        text = text.replace(now, was)
    return text


# Applied in both states; D6 and the test seam are applied only for pre-fixes
# and pre-preset respectively.
CORE = [(FORMATTERS_NOW, FORMATTERS_WAS),
        (FIELDS_NOW, FIELDS_WAS),
        (FMT_NOW, FMT_WAS)]
PRESET = [(PRESET_NOW, PRESET_WAS),
          (PRESET_LINE_NOW, PRESET_LINE_WAS)]
SEAM = [(EXPORTS_NOW, EXPORTS_WAS)]


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("pre-fixes", "pre-preset", "final"):
        print(__doc__)
        return 2
    state = sys.argv[1]

    if not os.path.exists(BAK):
        shutil.copy2(APP, BAK)

    if state == "final":
        shutil.copy2(BAK, APP)
        os.remove(BAK)
        print("restored the shipped app.js")
        return 0

    final = open(BAK, encoding="utf-8").read()
    if state == "pre-fixes":
        out = rewind(final, CORE + PRESET + SEAM, state)
    else:                                    # pre-preset: D1-D5 already in
        out = rewind(final, PRESET + SEAM, state)

    open(APP, "w", encoding="utf-8").write(out)
    print(f"app.js rewound to '{state}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
