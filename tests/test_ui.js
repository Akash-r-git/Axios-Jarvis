/* ======================================================================
   Interface unit tests — the pure functions in server/static/app.js.
   Run with:  node tests/test_ui.js      (or: python3 run.py --check)

   Every test here corresponds to a defect that actually shipped and was
   found by driving the real page in a browser. They exist so the same
   class of bug cannot come back silently. See TASKMANAGER.md.
   ====================================================================== */
'use strict';

const path = require('path');

/* app.js is a browser script. Stub just enough DOM for it to load; the
   functions under test are pure and touch none of it. */
const noop = () => {};
const fakeEl = () => ({
  style: {}, dataset: {}, classList: { add: noop, remove: noop, toggle: noop },
  setAttribute: noop, appendChild: noop, replaceChildren: noop,
  addEventListener: noop, textContent: '', children: []
});
global.document = {
  addEventListener: noop, createTextNode: () => fakeEl(),
  createElement: () => fakeEl(), createElementNS: () => fakeEl(),
  querySelector: () => null, querySelectorAll: () => [],
  getElementById: () => null, body: fakeEl()
};
global.window = { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener: noop }) };
global.requestAnimationFrame = noop;
global.fetch = () => Promise.reject(new Error('no network in unit tests'));

const A = require(path.join(__dirname, '..', 'server', 'static', 'app.js'));

let pass = 0, fail = 0;
const G = '\x1b[32m', R = '\x1b[31m', Z = '\x1b[0m';

function check(name, fn) {
  try {
    fn();
    pass++; console.log(`  ${G}PASS${Z}  ${name}`);
  } catch (e) {
    fail++; console.log(`  ${R}FAIL${Z}  ${name}\n          ${e.message}`);
  }
}
function eq(got, want, what = '') {
  if (got !== want) {
    throw new Error(`${what} expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
  }
}
function ok(cond, msg) { if (!cond) throw new Error(msg || 'expected truthy'); }

console.log('\n  Maestro Twin — interface tests');
console.log('  ' + '-'.repeat(66));

/* ------------------------------------------------------------------ D3 --
   A missing number must never render as a number. The engine sends null
   for Infinity and NaN because neither is legal JSON, and a metric whose
   baseline mean is zero has an undefined percentage. Before the fix,
   sgn(null) printed "+0.00" — a fabricated figure in a judged demo. */
check('A missing value never renders as a number', () => {
  for (const bad of [null, undefined, NaN, Infinity, -Infinity]) {
    eq(A.sgn(bad), '—', `sgn(${String(bad)})`);
    eq(A.n1(bad), '—', `n1(${String(bad)})`);
    eq(A.n2(bad), '—', `n2(${String(bad)})`);
    eq(A.pct(bad), '—', `pct(${String(bad)})`);
    ok(A.missing(bad), `missing(${String(bad)}) should be true`);
  }
});

check('Real numbers still format with an explicit sign', () => {
  eq(A.sgn(27.4), '+27.40');
  eq(A.sgn(-0.19), '-0.19');
  eq(A.sgn(0), '+0.00');
  eq(A.sgn(29.7, 1), '+29.7');
  ok(!A.missing(0), 'zero is a real value, not missing');
});

check('money() says "none" for no capital and never crashes', () => {
  eq(A.money(0), 'none');
  eq(A.money(null), 'none');
  eq(A.money(undefined), 'none');
  eq(A.money(NaN), 'none');
  ok(/240/.test(A.money(240000)), 'should render 240000 with a group separator');
});

/* ------------------------------------------------------------------ D1 --
   The defect that took the whole What-if tab down: the field table read
   s.yield_rate, but to_dict() emits the JSON key "yield" (the Python
   attribute is yield_rate only because `yield` is reserved). An undefined
   here reached .toFixed and threw inside renderTab(), which runs before
   runWhatIf() in the preset handler — so the request never even fired. */
check('Every editable field resolves against the JSON wire format', () => {
  const wireStage = {
    id: 'S3', name: 'CNC Machining', machines: 2,
    proc: { kind: 'lognorm', mean: 55.0, cv: 0.25 },
    in_buffer: 8, mtbf: 3600.0,
    repair: { kind: 'lognorm', mean: 600.0, cv: 0.5 },
    yield: 0.97, setup_time: 180.0, batch_size: 40
  };
  for (const F of A.FIELDS) {
    const v = F.get(wireStage);
    ok(!A.missing(v), `field ${F.f} read undefined off the wire format`);
    eq(typeof v, 'number', `field ${F.f} should read a number`);
  }
});

check('The patch field names match the engine whitelist', () => {
  /* dtwin/whatif.py PATCHABLE. Kept in sync by hand; this test is the
     tripwire if either side drifts. */
  const PATCHABLE = new Set(['proc.mean', 'proc.cv', 'machines', 'in_buffer',
                             'mtbf', 'repair.mean', 'yield_rate',
                             'setup_time', 'batch_size']);
  for (const F of A.FIELDS) {
    ok(PATCHABLE.has(F.f), `field ${F.f} is not patchable by the engine`);
  }
});

/* ------------------------------------------------------------------ D2 --
   fmtField must be total. A future key drift should degrade to an em dash,
   not throw and blank the tab it was rendering. */
check('fmtField degrades instead of throwing on a missing value', () => {
  const F = A.FIELDS.find(f => f.f === 'proc.cv');
  eq(A.fmtField(F, undefined), '—');
  eq(A.fmtField(F, null), '—');
  eq(A.fmtField(F, NaN), '—');
  eq(A.fmtField(F, 0.25), '0.250');
});

check('fmtField respects each field step and unit', () => {
  const cycle = A.FIELDS.find(f => f.f === 'proc.mean');   // step 0.5
  const mach = A.FIELDS.find(f => f.f === 'machines');     // step 1
  eq(A.fmtField(cycle, 55), '55.00 s');
  eq(A.fmtField(mach, 3), '3');
});

/* ------------------------------------------------------------------ D4 --
   An unlimited input buffer is carried on the wire as -1 (Infinity is not
   legal JSON). It used to display as "-1 slots". */
check('An unlimited buffer reads as unlimited, not as -1 slots', () => {
  const F = A.FIELDS.find(f => f.f === 'in_buffer');
  ok(A.UNLIMITED(-1) && A.UNLIMITED(null) && A.UNLIMITED(undefined),
     'the sentinel, null and undefined all mean unlimited');
  ok(!A.UNLIMITED(0) && !A.UNLIMITED(8), 'a real capacity is not unlimited');
  eq(F.get({ in_buffer: -1 }), 0, 'unlimited clamps to 0 for the slider');
  eq(A.fmtField(F, 0), 'unlimited');
  eq(A.fmtField(F, 8), '8 slots');
});

/* ------------------------------------------------------------------ D5 --
   The slider preview must agree with what the engine actually did.
   dtwin/whatif.py treats an unlimited buffer as 0 under `mul` and `add`. */
check('The slider preview matches the engine patch arithmetic', () => {
  eq(A.patchedValue(55, { op: 'set', value: 60 }), 60);
  eq(A.patchedValue(55, { op: 'mul', value: 1.2 }), 66);
  eq(A.patchedValue(2, { op: 'add', value: 1 }), 3);
  eq(A.patchedValue(null, { op: 'add', value: 10 }), 10,
     'unlimited + 10 is 10, matching apply_patches');
  eq(A.patchedValue(undefined, { op: 'mul', value: 2 }), 0);
});

check('describePatch renders every operator without throwing', () => {
  const out = [
    A.describePatch({ stage: 'S3', field: 'proc.mean', op: 'mul', value: 1.2 }),
    A.describePatch({ stage: 'S3', field: 'machines', op: 'add', value: 1 }),
    A.describePatch({ stage: 'S3', field: 'yield_rate', op: 'set', value: 0.99 })
  ];
  for (const s of out) {
    ok(s.startsWith('S3 '), `patch label should name the stage: ${s}`);
    ok(!/undefined|NaN/.test(s), `patch label leaked a bad value: ${s}`);
  }
  ok(/×1\.20/.test(out[0]), `mul should show a multiplier: ${out[0]}`);
  ok(/\+1/.test(out[1]), `add should show a signed delta: ${out[1]}`);
});

/* ------------------------------------------------------------------ D6 --
   The "should do nothing" preset used to hard-code the LAST stage. On the
   pharma line the last stage IS the constraint, so the button labelled
   "the change that should do nothing" returned a significant +7.3 units/h.
   It now targets whichever stage has the most spare capacity. */
check('The null-effect preset targets a non-constraint on any topology', () => {
  const preset = A.PRESETS[A.PRESETS.length - 1];
  ok(/non-constraint/i.test(preset.t), 'preset should be named for what it does');

  /* Constraint last (the pharma shape that exposed the bug). */
  const seed = (table) => { A.__state.baseline = { report: { losses: { capacity_table: table } } }; };
  seed([{ id: 'S1', r_good: 150 }, { id: 'S2', r_good: 260 },
        { id: 'S3', r_good: 190 }, { id: 'S6', r_good: 118 }]);
  eq(preset.build('S6')[0].stage, 'S2', 'should pick the slackest stage');

  /* Constraint in the middle (the moulding shape). */
  seed([{ id: 'S1', r_good: 140 }, { id: 'S3', r_good: 93 }, { id: 'S6', r_good: 176 }]);
  eq(preset.build('S3')[0].stage, 'S6');
  ok(preset.build('S3')[0].value < 1, 'should be a speed-up, not a slow-down');
});

check('Every preset builds a patch the engine can accept', () => {
  A.__state.baseline = { report: { losses: { capacity_table: [
    { id: 'S1', r_good: 140 }, { id: 'S3', r_good: 93 }, { id: 'S6', r_good: 176 }
  ] } } };
  const fields = new Set(A.FIELDS.map(f => f.f));
  for (const p of A.PRESETS) {
    const patches = p.build('S3');
    ok(Array.isArray(patches) && patches.length, `${p.t} built nothing`);
    for (const q of patches) {
      ok(typeof q.stage === 'string' && q.stage, `${p.t}: no stage`);
      ok(fields.has(q.field), `${p.t}: unknown field ${q.field}`);
      ok(['set', 'mul', 'add'].includes(q.op), `${p.t}: bad op ${q.op}`);
      ok(typeof q.value === 'number' && Number.isFinite(q.value),
         `${p.t}: value is not a finite number`);
      /* The exact crash shape: describePatch reads p.value directly. */
      ok(!/undefined|NaN/.test(A.describePatch(q)), `${p.t}: label leaked a bad value`);
    }
  }
});

check('Advisor levers map back to concrete patches', () => {
  for (const label of ['+1 parallel machine', '20% faster cycle time',
                       'MTTR cut 50%', '+5 buffer slots']) {
    const patches = A.leverPatches({ stage: 'S3', label });
    ok(Array.isArray(patches) && patches.length, `no patch for "${label}"`);
    for (const q of patches) {
      ok(A.FIELDS.some(f => f.f === q.field), `"${label}" produced field ${q.field}`);
      ok(Number.isFinite(q.value), `"${label}" produced a non-finite value`);
    }
  }
});

check('Playback frames decode to the shift clock', () => {
  eq(A.hhmm(0), '00:00');
  eq(A.hhmm(3661), '01:01');
  eq(A.hhmm(28800), '08:00');
  eq(A.hhmm(-5), '00:00', 'negative time clamps to zero');
});

check('deltaClass reads improvement in the right direction per metric', () => {
  /* More throughput is good; more cycle time is not. */
  ok(A.deltaClass('throughput_per_h', 5) !== A.deltaClass('throughput_per_h', -5),
     'sign should change the class');
  ok(A.deltaClass('cycle_time_s', -5) === A.deltaClass('throughput_per_h', 5),
     'less cycle time and more throughput are both improvements');
});

console.log('  ' + '-'.repeat(66));
console.log(`  ${pass} passed, ${fail} failed\n`);
process.exit(fail === 0 ? 0 : 1);
