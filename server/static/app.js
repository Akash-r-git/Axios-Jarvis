/* ==========================================================================
   Maestro Twin — frontend
   No framework, no build step, no CDN. Loads from the same Python process that
   runs the simulation, so the whole product works offline.
   ========================================================================== */
'use strict';

const STATE_KEYS = ['busy', 'setup', 'down', 'blocked', 'starved'];
const STATE_COLOR = {
  busy: '#3FB950', setup: '#D29922', down: '#F85149',
  blocked: '#DB6D28', starved: '#59606B'
};
const STATE_WORD = {
  busy: 'producing', setup: 'in changeover', down: 'broken down',
  blocked: 'blocked', starved: 'waiting'
};

const S = {
  session: null, plant: null, baseline: null, advisor: null,
  calibration: null, whatif: null,
  tab: 'diagnose', selected: null, patches: [],
  frames: null, frameIdx: 0, playing: false, timer: null,
  compareFrames: null, lineExpanded: false
};

/* ---------------------------------------------------------------- helpers */
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, kids = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of [].concat(kids)) {
    if (c === null || c === undefined) continue;
    n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return n;
};
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs = {}, kids = []) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') { n.textContent = v; continue; }
    if (k.startsWith('on')) { n.addEventListener(k.slice(2), v); continue; }
    n.setAttribute(k, v);
  }
  for (const c of [].concat(kids)) if (c) n.appendChild(c);
  return n;
};

/* A missing number must never render as a number. The engine sends null for
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
  ? 'none' : Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 });
const hhmm = (sec) => {
  const s = Math.max(0, Math.round(sec));
  return String(Math.floor(s / 3600)).padStart(2, '0') + ':' +
         String(Math.floor((s % 3600) / 60)).padStart(2, '0');
};

function toast(msg, ms = 2600) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const t = el('div', { class: 'toast', text: msg });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function errorBox(where, e) {
  return el('div', { class: 'errorbox' }, [
    el('b', { text: where }), el('br'),
    el('span', { text: String(e && e.message ? e.message : e) })
  ]);
}

async function api(path, opts = {}) {
  // accept a plain object as the body as well as a pre-serialised string
  if (opts.body && typeof opts.body !== 'string') {
    opts = { ...opts, body: JSON.stringify(opts.body) };
  }
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  });
  let data;
  try { data = await res.json(); }
  catch { throw new Error(`Server returned ${res.status} with a non-JSON body.`); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

/* ====================================================================
   Launch
   ==================================================================== */
async function bootLaunch() {
  const cards = $('plantCards');
  cards.replaceChildren(el('div', { class: 'empty-state', text: 'Loading lines…' }));
  try {
    const cat = await api('/api/catalog');
    cards.replaceChildren(...cat.plants.map(p => {
      const arch = cat.archetypes.find(a => a.key === p.archetype);
      return el('button', { class: 'card', onclick: () => openLine({ plant_file: p.file }) }, [
        el('h3', { text: p.name }),
        el('p', { text: arch ? arch.industry : 'Multi-stage production line' }),
        el('div', { class: 'meta' }, [
          el('span', { text: `${p.stages} stages` }),
          el('span', { text: '·' }),
          el('span', { text: arch ? `typical OEE ${pct(arch.typical_oee)}` : 'custom' })
        ])
      ]);
    }));
  } catch (e) {
    cards.replaceChildren(errorBox('Could not load the line catalogue.', e));
  }
}

async function describeCompany() {
  const q = $('describeInput').value.trim();
  if (!q) { toast('Describe the plant first — industry, product, process.'); return; }
  const box = $('matches');
  box.className = 'matches show';
  box.replaceChildren(el('div', { class: 'panel' }, [el('span', { class: 'spinner' })]));
  try {
    const r = await api('/api/session', {
      method: 'POST', body: JSON.stringify({ describe: q })
    });
    S.session = r.session;
    const rows = r.matches.map(m => el('tr', { class: m.key === r.archetype ? '' : 'muted' }, [
      el('td', {}, [el('b', { text: m.label })]),
      el('td', { text: m.industry }),
      el('td', { class: 'num', text: m.score ? m.matched_terms.join(', ') : 'no signal' })
    ]));
    box.replaceChildren(el('div', { class: 'panel' }, [
      el('h3', { text: `Matched: ${r.matches[0].label}` }),
      el('p', {
        class: 'hint',
        text: 'Matched on the words in your description. Cycle times and breakdown ' +
              'rates come from the archetype, not from your company — they are marked ' +
              'as estimates until you calibrate.'
      }),
      el('table', {}, [el('tbody', {}, rows)]),
      el('div', { style: 'margin-top:14px' }, [
        el('button', { class: 'btn primary', text: 'Open this twin', onclick: () => enterWorkspace(r) })
      ])
    ]));
  } catch (e) {
    box.replaceChildren(errorBox('Archetype matching failed.', e));
  }
}

async function openLine(body) {
  try {
    const r = await api('/api/session', { method: 'POST', body: JSON.stringify(body) });
    enterWorkspace(r);
  } catch (e) {
    $('plantCards').prepend(errorBox('Could not open that line.', e));
  }
}

function enterWorkspace(r) {
  S.session = r.session;
  S.plant = r.plant;
  S.provenance = r.provenance;
  S.provSummary = r.provenance_summary;
  S.patches = []; S.whatif = null; S.advisor = null; S.calibration = null;
  $('launch').hidden = true;
  $('workspace').hidden = false;
  $('btnChangeLine').hidden = false;
  $('lineName').textContent = r.plant.name;
  renderProvChip();
  loadBaseline();
  pollAdvisor();
}

/* ====================================================================
   Baseline
   ==================================================================== */
async function loadBaseline() {
  $('tabbody').replaceChildren(el('div', { class: 'empty-state' }, [
    el('b', { text: 'Simulating the shift' }),
    el('span', { text: 'Ten replications of an eight-hour shift, a few seconds.' })
  ]));
  try {
    S.baseline = await api(`/api/session/${S.session}/baseline?reps=10`);
    S.plant = S.baseline.plant;
    S.provenance = S.baseline.provenance;
    S.provSummary = S.baseline.provenance_summary;
    S.frames = decodeFrames(S.baseline.frames);
    S.frameIdx = S.frames.count - 1;
    S.compareFrames = null;
    if (!S.selected) S.selected = S.baseline.report.bottlenecks[0].id;
    renderTopChips();
    renderProvChip();
    buildLine();
    renderScrub();
    renderTab();
    renderRail();
  } catch (e) {
    $('tabbody').replaceChildren(errorBox('The simulation did not complete.', e));
  }
}

function decodeFrames(f) {
  if (!f || !f.count) return null;
  return {
    count: f.count, stride: f.stride, interval: f.interval,
    stages: f.stages, data: f.data,
    at(i) {
      const o = i * this.stride, d = this.data;
      const st = this.stages.map((id, j) => {
        const b = o + 3 + j * 6;
        return {
          id, buffer: d[b],
          counts: { busy: d[b + 1], setup: d[b + 2], down: d[b + 3], blocked: d[b + 4], starved: d[b + 5] }
        };
      });
      return { t: d[o], good: d[o + 1], scrap: d[o + 2], stages: st };
    }
  };
}

function renderTopChips() {
  const b = S.baseline, rep = b.replications;
  const rc = $('repChip');
  rc.hidden = false;
  rc.textContent = `${n1(rep.mean)} ± ${n1(rep.ci95)} units/h over ${rep.reps} runs`;
  rc.title = `Run-to-run standard deviation ${n2(rep.sd)} units/h. A single ` +
             `before/after comparison could differ by this much with nothing changed.`;

  const v = b.report.validation, passed = v.filter(c => c.pass).length;
  const vc = $('validChip');
  vc.hidden = false;
  vc.className = 'chip action ' + (passed === v.length ? 'ok' : 'bad');
  vc.replaceChildren(el('span', { class: 'dot' }),
                     el('span', { text: `${passed}/${v.length} checks` }));
  vc.onclick = () => showDialog('Internal consistency checks',
    el('div', {}, [
      el('p', { text: 'These run on every simulation. If any fails, the numbers on this screen should not be trusted.' }),
      el('table', {}, [el('tbody', {}, v.map(c => el('tr', {}, [
        el('td', {}, [el('span', { class: 'pill ' + (c.pass ? 'sig' : 'noise'), text: c.pass ? 'pass' : 'fail' })]),
        el('td', { text: c.name }),
        el('td', { class: 'num', text: c.value })
      ])))])
    ]));
}

function renderProvChip() {
  const ps = S.provSummary;
  if (!ps) return;
  const c = $('provChip');
  c.hidden = false;
  const m = ps.counts.measured, t = ps.total;
  c.className = 'chip ' + (m === t ? 'ok' : m > 0 ? 'warn' : '');
  c.textContent = m === t ? 'Calibrated' :
                  m > 0 ? `Part calibrated (${Math.round(m / t * 100)}%)` : 'Not calibrated';
  c.title = ps.verdict;
}

/* ====================================================================
   The line — the hero element
   ==================================================================== */
const GEO = { padX: 22, padY: 34, bw: 156, gap: 84, bh: 126 };

function buildLine() {
  const svg = $('lineSvg');
  svg.replaceChildren();
  const stages = S.baseline.report.kpis.stages;
  const bnId = S.baseline.report.bottlenecks[0].id;
  const SINK_W = 118;
  const W = GEO.padX * 2 + stages.length * GEO.bw + (stages.length - 1) * GEO.gap + SINK_W;
  const H = GEO.padY + GEO.bh + 42;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  S.lineRefs = { machines: {}, buffers: {}, particles: [] };

  stages.forEach((st, i) => {
    const x = GEO.padX + i * (GEO.bw + GEO.gap);
    if (i > 0) svg.appendChild(buildConveyor(x - GEO.bw - GEO.gap, x, st, i));
    svg.appendChild(buildBlock(st, x, i, st.id === bnId));
  });
  svg.appendChild(buildSink(GEO.padX + (stages.length - 1) * (GEO.bw + GEO.gap) + GEO.bw + 6));
  paintFrame(S.frameIdx);
}

function buildBlock(st, x, idx, isBn) {
  const y = GEO.padY;
  const plantStage = S.plant.stages[idx];
  const g = sv('g', { class: 'stage', style: 'cursor:pointer', onclick: () => selectStage(st.id) });

  g.appendChild(sv('rect', {
    x, y, width: GEO.bw, height: GEO.bh, rx: 8,
    fill: '#161C26',
    stroke: isBn ? '#5B8DB8' : '#293240',
    'stroke-width': isBn ? 2 : 1
  }));

  if (isBn) {
    g.appendChild(sv('rect', { x: x + 10, y: y - 25, width: 92, height: 19, rx: 9.5,
                               fill: '#33536E', stroke: '#5B8DB8' }));
    g.appendChild(sv('text', { x: x + 56, y: y - 11.5, 'text-anchor': 'middle',
                               'font-size': 10.5, fill: '#EAF2F8', 'font-weight': 600,
                               text: 'Constraint' }));
  }

  g.appendChild(sv('text', { x: x + 12, y: y + 19, 'font-size': 10.5, fill: '#6E7681',
                             'letter-spacing': .4, text: st.id }));
  const nameEl = sv('text', { x: x + 12, y: y + 34, 'font-size': 12.5, fill: '#E6EDF3',
                              'font-weight': 550, text: clip(st.name, 21) });
  nameEl.appendChild(sv('title', { text: st.name }));
  g.appendChild(nameEl);

  // live machine chips — one per physical machine
  const n = plantStage.machines;
  const cw = Math.min(26, (GEO.bw - 24 - (n - 1) * 4) / n);
  const chips = [];
  for (let m = 0; m < n; m++) {
    const r = sv('rect', {
      x: x + 12 + m * (cw + 4), y: y + 44, width: cw, height: 14, rx: 3,
      fill: STATE_COLOR.starved
    });
    chips.push(r); g.appendChild(r);
  }
  S.lineRefs.machines[st.id] = chips;

  // shift-aggregate state band — the analysis, always visible
  const bw = GEO.bw - 24;
  let cx = x + 12;
  for (const k of STATE_KEYS) {
    const w = Math.max(0, st[k]) * bw;
    if (w > 0.2) {
      const r = sv('rect', { x: cx, y: y + 66, width: w, height: 9, fill: STATE_COLOR[k] });
      r.appendChild(sv('title', { text: `${pct(st[k], 1)} ${STATE_WORD[k]}` }));
      g.appendChild(r);
    }
    cx += w;
  }
  g.appendChild(sv('rect', { x: x + 12, y: y + 66, width: bw, height: 9, rx: 2,
                             fill: 'none', stroke: '#11161E' }));

  const dom = STATE_KEYS.map(k => [k, st[k]]).sort((a, b) => b[1] - a[1])[0];
  g.appendChild(sv('text', { x: x + 12, y: y + 91, 'font-size': 11.5, fill: '#E6EDF3',
                             text: `${pct(st.busy)} producing` }));
  g.appendChild(sv('text', {
    x: x + 12, y: y + 106, 'font-size': 10.5, fill: '#6E7681',
    text: dom[0] === 'busy' ? `${n} machine${n > 1 ? 's' : ''} · ${n1(plantStage.proc.mean)}s cycle`
                            : `${pct(dom[1])} ${STATE_WORD[dom[0]]}`
  }));
  g.appendChild(sv('text', { x: x + 12, y: y + 119, 'font-size': 10.5, fill: '#5B8DB8',
                             text: st.bn_share > 0.02 ? `constraint ${pct(st.bn_share)} of shift` : '' }));
  return g;
}

function buildConveyor(xPrev, xNext, st, idx) {
  const g = sv('g');
  const y = GEO.padY + GEO.bh / 2;
  const x0 = xPrev + GEO.bw, x1 = xNext;
  g.appendChild(sv('line', { x1: x0, y1: y, x2: x1, y2: y, stroke: '#293240', 'stroke-width': 2 }));
  g.appendChild(sv('path', {
    d: `M ${x1 - 8} ${y - 4} L ${x1} ${y} L ${x1 - 8} ${y + 4} Z`, fill: '#293240'
  }));

  // buffer slots — the physical thing that decouples two stages
  const cap = st.buf_cap;
  const shown = cap === null ? 6 : Math.min(10, Math.max(1, Math.round(cap)));
  const sw = 7, sg = 2.5;
  const totalW = shown * sw + (shown - 1) * sg;
  const bx = (x0 + x1) / 2 - totalW / 2;
  g.appendChild(sv('rect', {
    x: bx - 5, y: y - 32, width: totalW + 10, height: 24, rx: 4,
    fill: '#11161E', stroke: '#293240'
  }));
  const slots = [];
  for (let i = 0; i < shown; i++) {
    const r = sv('rect', { x: bx + i * (sw + sg), y: y - 27, width: sw, height: 14, rx: 1.5,
                           fill: 'none', stroke: '#38424f' });
    slots.push(r); g.appendChild(r);
  }
  const lbl = sv('text', { x: (x0 + x1) / 2, y: y - 37, 'text-anchor': 'middle',
                           'font-size': 10, fill: '#6E7681', text: '' });
  g.appendChild(lbl);
  S.lineRefs.buffers[st.id] = { slots, label: lbl, cap, shown };
  return g;
}

function buildSink(x) {
  const y = GEO.padY + GEO.bh / 2;
  const g = sv('g');
  g.appendChild(sv('line', { x1: x, y1: y, x2: x + 52, y2: y, stroke: '#293240', 'stroke-width': 2 }));
  const t = sv('text', { x: x + 56, y: y + 4, 'font-size': 11, fill: '#6E7681', text: 'finished' });
  g.appendChild(t);
  S.lineRefs.sink = t;
  return g;
}

const clip = (s, n) => (s || '').length > n ? s.slice(0, n - 1) + '…' : (s || '');

function paintFrame(i) {
  if (!S.frames || !S.lineRefs) return;
  const f = S.frames.at(Math.max(0, Math.min(S.frames.count - 1, i)));
  for (const st of f.stages) {
    const chips = S.lineRefs.machines[st.id];
    if (chips) {
      // paint one chip per machine, in a fixed state order so a machine does
      // not appear to jump between positions frame to frame
      let k = 0;
      for (const key of STATE_KEYS) {
        for (let c = 0; c < st.counts[key] && k < chips.length; c++, k++) {
          chips[k].setAttribute('fill', STATE_COLOR[key]);
        }
      }
    }
    const buf = S.lineRefs.buffers[st.id];
    if (buf) {
      const lvl = st.buffer;
      const capForScale = buf.cap === null ? Math.max(1, lvl) : buf.cap;
      const filled = Math.round(Math.min(1, lvl / capForScale) * buf.shown);
      buf.slots.forEach((r, j) => {
        const on = j < filled;
        r.setAttribute('fill', on ? (filled === buf.shown ? '#DB6D28' : '#5B8DB8') : 'none');
        r.setAttribute('stroke', on ? 'none' : '#38424f');
      });
      buf.label.textContent = buf.cap === null ? `${lvl} waiting` : `${lvl}/${buf.cap}`;
    }
  }
  if (S.lineRefs.sink) S.lineRefs.sink.textContent = `${f.good} finished`;
  $('clock').innerHTML = `shift <b>${hhmm(f.t)}</b> · ${f.good} units · ${f.scrap} scrapped`;
  const sc = $('scrub');
  if (Number(sc.value) !== i) sc.value = i;
}

function renderScrub() {
  const sc = $('scrub');
  sc.max = S.frames ? S.frames.count - 1 : 0;
  sc.value = S.frameIdx;
  paintFrame(S.frameIdx);
}

function togglePlay() {
  S.playing = !S.playing;
  $('btnPlay').textContent = S.playing ? 'Pause' : 'Play shift';
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  if (!S.playing) return;
  if (S.frameIdx >= S.frames.count - 1) S.frameIdx = 0;
  const fps = Number($('speed').value);
  S.timer = setInterval(() => {
    S.frameIdx++;
    if (S.frameIdx >= S.frames.count) { S.frameIdx = S.frames.count - 1; togglePlay(); return; }
    paintFrame(S.frameIdx);
  }, 1000 / fps);
}

function toggleLineExpanded() {
  S.lineExpanded = !S.lineExpanded;
  $('workspace').classList.toggle('line-expanded', S.lineExpanded);
  $('btnExpandLine').textContent = S.lineExpanded ? 'Collapse line' : 'Expand line';
  $('btnExpandLine').setAttribute('aria-pressed', S.lineExpanded ? 'true' : 'false');
  if (S.baseline) requestAnimationFrame(buildLine);
}

/* ====================================================================
   Charts (hand-rolled SVG — no chart library)
   ==================================================================== */
function waterfall(buckets) {
  const W = 620, H = 210, padL = 8, padB = 46, padT = 16;
  const max = Math.max(...buckets.map(b => b[1])) * 1.08;
  const bw = (W - padL * 2) / buckets.length;
  const g = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: 'display:block' });
  const yOf = v => padT + (1 - v / max) * (H - padT - padB);

  buckets.forEach((b, i) => {
    const [name, level, loss] = b;
    const x = padL + i * bw;
    const y = yOf(level);
    const isLast = i === buckets.length - 1;
    g.appendChild(sv('rect', {
      x: x + bw * 0.16, y, width: bw * 0.68, height: H - padB - y, rx: 3,
      fill: isLast ? '#3FB950' : (i === 0 ? '#33536E' : '#293240'),
      stroke: isLast ? '#3FB950' : '#38424f', 'stroke-width': 1, 'fill-opacity': isLast ? .35 : 1
    }));
    if (i > 0) {
      const py = yOf(buckets[i - 1][1]);
      g.appendChild(sv('line', {
        x1: x - bw * 0.16, y1: py, x2: x + bw * 0.16, y2: py,
        stroke: '#6E7681', 'stroke-dasharray': '3 3'
      }));
      if (loss > 0.05) {
        g.appendChild(sv('text', {
          x, y: py - 6, 'text-anchor': 'middle', 'font-size': 10.5,
          fill: '#F85149', text: `−${n1(loss)}`
        }));
      }
    }
    g.appendChild(sv('text', {
      x: x + bw * 0.5, y: y - 6, 'text-anchor': 'middle', 'font-size': 12,
      fill: '#E6EDF3', 'font-weight': 600, text: n1(level)
    }));
    wrapText(g, shortBucket(name), x + bw * 0.5, H - padB + 15, bw * 0.96, 10);
  });
  return g;
}

function shortBucket(s) {
  return s.replace('Nameplate rate of slowest stage', 'Nameplate')
          .replace('Breakdown loss', 'After breakdowns')
          .replace('Changeover loss', 'After changeovers')
          .replace('Scrap / yield loss', 'After scrap')
          .replace('Flow loss (variability + blocking + starvation)', 'Actually delivered');
}

function wrapText(g, text, cx, y, maxW, size) {
  const words = String(text).split(' ');
  const perLine = Math.max(1, Math.floor(maxW / (size * 0.56)));
  const lines = []; let cur = '';
  for (const w of words) {
    if ((cur + ' ' + w).trim().length > perLine && cur) { lines.push(cur); cur = w; }
    else cur = (cur + ' ' + w).trim();
  }
  if (cur) lines.push(cur);
  lines.slice(0, 2).forEach((l, i) => g.appendChild(sv('text', {
    x: cx, y: y + i * (size + 2), 'text-anchor': 'middle', 'font-size': size,
    fill: '#9DA7B3', text: l
  })));
}

function hbars(rows, opts = {}) {
  const { valueKey = 'value', labelKey = 'label', fmt = n2, suffix = '', color = '#5B8DB8' } = opts;
  const max = Math.max(1e-9, ...rows.map(r => Math.abs(r[valueKey])));
  return el('div', {}, rows.map(r => {
    const w = Math.abs(r[valueKey]) / max * 100;
    return el('div', { style: 'margin-bottom:9px' }, [
      el('div', { style: 'display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px' }, [
        el('span', { text: r[labelKey] }),
        el('span', { style: 'color:var(--ink-2)', text: fmt(r[valueKey]) + suffix })
      ]),
      el('div', { style: 'height:7px;background:var(--sunken);border-radius:4px;overflow:hidden' }, [
        el('div', { style: `height:100%;width:${w}%;background:${r.color || color};border-radius:4px` })
      ])
    ]);
  }));
}

function sparkline(points, opts = {}) {
  const W = 320, H = 96, pad = 20;
  const max = Math.max(...points.map(p => p.y)) * 1.1 || 1;
  const g = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: 'display:block' });
  const xOf = i => pad + i * (W - pad * 2) / Math.max(1, points.length - 1);
  const yOf = v => pad / 2 + (1 - v / max) * (H - pad * 1.6);
  g.appendChild(sv('line', { x1: pad, y1: H - pad, x2: W - pad, y2: H - pad, stroke: '#293240' }));
  const d = points.map((p, i) => `${i ? 'L' : 'M'} ${xOf(i)} ${yOf(p.y)}`).join(' ');
  g.appendChild(sv('path', { d, fill: 'none', stroke: opts.color || '#5B8DB8', 'stroke-width': 2 }));
  points.forEach((p, i) => {
    g.appendChild(sv('circle', { cx: xOf(i), cy: yOf(p.y), r: 3, fill: opts.color || '#5B8DB8' }));
    g.appendChild(sv('text', { x: xOf(i), y: H - pad + 13, 'text-anchor': 'middle',
                               'font-size': 9.5, fill: '#6E7681', text: p.x }));
  });
  return g;
}

/* ====================================================================
   Tabs
   ==================================================================== */
function renderTab() {
  const body = $('tabbody');
  body.replaceChildren();
  if (!S.baseline) return;
  ({ diagnose: tabDiagnose, whatif: tabWhatIf, advisor: tabAdvisor,
     calibrate: tabCalibrate, line: tabLineSetup,
     live: (typeof tabLive === 'function' ? tabLive : tabDiagnose),
     sources: (typeof tabSources === 'function' ? tabSources : tabDiagnose)
   }[S.tab] || tabDiagnose)(body);
}

/* ---------------------------------------------------------- Diagnose ---- */
function tabDiagnose(body) {
  const r = S.baseline.report, k = r.kpis, L = r.losses, rep = S.baseline.replications;

  body.appendChild(el('div', { class: 'kpis' }, [
    kpi('Throughput', n1(rep.mean), 'good units/h', `±${n1(rep.ci95)} at 95% over ${rep.reps} runs`),
    kpi('Constraint', r.bottlenecks[0].id, '', `${r.bottlenecks[0].name} · ${pct(r.bottlenecks[0].bn_share)} of shift`),
    kpi('Work in progress', n1(k.wip), 'units', `cycle time ${n0(k.cycle_time_s)}s`),
    kpi('Flow ratio', n2(k.flow_ratio), '×', `${pct(1 - 1 / Math.max(k.flow_ratio, 1.001))} of a unit's life is waiting`),
    kpi('First-pass yield', pct(k.first_pass_yield, 1), '', `${k.scrap_total} units scrapped`),
    kpi('Of design capacity', pct(L.overall_efficiency), '', `${n1(L.structural_capacity)} structural ceiling`)
  ]));

  body.appendChild(el('div', { class: 'grid2' }, [
    el('div', { class: 'panel' }, [
      el('h3', { text: 'Where the capacity goes' }),
      el('p', { class: 'hint', text:
        'Each step removes one loss mechanism. The first four are arithmetic — a spreadsheet ' +
        'can do them. The last one, the gap between structural capacity and what the line ' +
        'actually delivers, only a simulation can produce: it is variability colliding with ' +
        'finite buffers.' }),
      waterfall(L.buckets),
      el('p', { class: 'hint', style: 'margin:12px 0 0', text:
        `Flow efficiency ${pct(L.flow_efficiency, 1)} of structural capacity. ` +
        `Static capacity model independently names ${L.static_bottleneck} as the constraint.` })
    ]),
    el('div', { class: 'panel' }, [
      el('h3', { text: 'Downtime propagation' }),
      el('p', { class: 'hint', text:
        'Every idle second on the line is charged to a root cause by walking the chain of ' +
        'empty or full buffers. Amplification above 1.0 means a stage\'s breakdowns cost the ' +
        'line more time than the breakdown itself.' }),
      hbars(r.propagation.stages.filter(s => s.own_downtime_s > 0).map(s => ({
        label: `${s.id} ${s.name}`, value: s.amplification,
        color: s.amplification > 1 ? '#F85149' : '#5B8DB8'
      })), { suffix: '×' }),
      el('h3', { style: 'margin-top:16px', text: 'Largest idle flows' }),
      el('table', {}, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: 'Cause' }), el('th', { text: 'State' }),
          el('th', { text: 'Idles' }), el('th', { class: 'num', text: 'Minutes' })
        ])]),
        el('tbody', {}, r.propagation.flows.slice(0, 6).map(f => el('tr', {}, [
          el('td', { text: f.cause }),
          el('td', {}, [el('span', {
            style: `color:${STATE_COLOR[f.cause_state.toLowerCase()] || 'var(--ink-2)'}`,
            text: STATE_WORD[f.cause_state.toLowerCase()] || f.cause_state.toLowerCase()
          })]),
          el('td', { text: `${f.victim} ${STATE_WORD[f.victim_state.toLowerCase()] || ''}` }),
          el('td', { class: 'num', text: n0(f.seconds / 60) })
        ])))
      ])
    ])
  ]));

  body.appendChild(el('div', { class: 'panel flush' }, [
    el('div', { style: 'padding:16px 16px 0' }, [
      el('h3', { text: 'Bottleneck ranking' }),
      el('p', { class: 'hint', text:
        'Ranked mainly by how often a stage holds the longest uninterrupted active period — ' +
        'the constraint is the machine that never gets interrupted because everything else ' +
        'waits on it. A stage that is merely busy does not outrank one that actually ' +
        'constrains its neighbours.' })
    ]),
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Stage' }),
        el('th', { class: 'num', text: 'Bottleneck share' }),
        el('th', { class: 'num', text: 'Producing' }),
        el('th', { class: 'num', text: 'Broken' }),
        el('th', { class: 'num', text: 'Blocked' }),
        el('th', { class: 'num', text: 'Waiting' }),
        el('th', { class: 'num', text: 'Capacity' })
      ])]),
      el('tbody', {}, r.bottlenecks.map((b, i) => {
        const cap = L.capacity_table.find(c => c.id === b.id);
        return el('tr', { class: 'clickable', onclick: () => selectStage(b.id) }, [
          el('td', {}, [
            i === 0 ? el('span', { class: 'pill sig', style: 'margin-right:7px', text: 'constraint' }) : null,
            el('span', { text: `${b.id} ${b.name}` })
          ]),
          el('td', { class: 'num', text: pct(b.bn_share, 1) }),
          el('td', { class: 'num', text: pct(b.busy, 1) }),
          el('td', { class: 'num', text: pct(b.down, 1) }),
          el('td', { class: 'num', text: pct(b.blocked, 1) }),
          el('td', { class: 'num', text: pct(b.starved, 1) }),
          el('td', { class: 'num', text: cap ? n1(cap.r_good) + '/h' : '—' })
        ]);
      }))
    ])
  ]));
}

function kpi(k, v, unit, s) {
  return el('div', { class: 'kpi' }, [
    el('div', { class: 'k', text: k }),
    el('div', { class: 'v' }, [document.createTextNode(v), unit ? el('small', { text: unit }) : null]),
    el('div', { class: 's', text: s })
  ]);
}

/* ----------------------------------------------------------- What-if ---- */
/* The last preset is the one that should come back "within noise": speeding up
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

const PRESETS = [
  { t: 'Tool wear', d: '+20% cycle time at the constraint', build: bn => [{ stage: bn, field: 'proc.mean', op: 'mul', value: 1.2 }] },
  { t: 'Buy capacity', d: 'one more machine at the constraint', build: bn => [{ stage: bn, field: 'machines', op: 'add', value: 1 }] },
  { t: 'Maintenance blitz', d: 'halve repair time at the constraint', build: bn => [{ stage: bn, field: 'repair.mean', op: 'mul', value: 0.5 }] },
  { t: 'More buffer', d: '+10 slots in front of the constraint', build: bn => [{ stage: bn, field: 'in_buffer', op: 'add', value: 10 }] },
  { t: 'Worse reliability', d: 'double repair time at the constraint', build: bn => [{ stage: bn, field: 'repair.mean', op: 'mul', value: 2 }] },
  { t: 'Speed up a non-constraint', d: 'the change that should do nothing', build: bn => [{ stage: slackestStage(bn), field: 'proc.mean', op: 'mul', value: 1 / 1.3 }] }
];

/* `f` is the patch field name the engine accepts (dtwin/whatif.py PATCHABLE);
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
];

function tabWhatIf(body) {
  const bn = S.baseline.report.bottlenecks[0].id;
  const stage = S.plant.stages.find(s => s.id === S.selected) || S.plant.stages[0];

  body.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'Change something, then find out whether it mattered' }),
    el('p', { class: 'hint', text:
      'Each scenario runs against the baseline under common random numbers: the same seeds, ' +
      'and per-stage random streams so changing one stage does not disturb another\'s draws. ' +
      'The difference is a paired comparison with a 95% confidence interval, because a single ' +
      `re-run of this line varies by ±${n1(S.baseline.replications.ci95)} units an hour on its own.` }),
    el('div', { class: 'presets' }, PRESETS.map(p => el('button', { class: 'preset',
      onclick: () => { S.patches = p.build(bn); renderTab(); runWhatIf(); } }, [
      el('b', { text: p.t }), el('span', { text: p.d })
    ])))
  ]));

  const rows = FIELDS.map(F => {
    const cur = F.get(stage);
    const patch = S.patches.find(p => p.stage === stage.id && p.field === F.f);
    const val = patch ? patchedValue(cur, patch) : cur;
    const valBox = el('input', { class: 'val' + (patch ? ' changed' : ''), value: fmtField(F, val), readonly: 'readonly' });
    const range = el('input', {
      type: 'range', min: F.min, max: F.max, step: F.step, value: clampNum(val, F.min, F.max),
      oninput: (e) => { valBox.value = fmtField(F, Number(e.target.value)); valBox.className = 'val changed'; },
      onchange: (e) => setPatch(stage.id, F.f, Number(e.target.value), cur)
    });
    return el('div', { class: 'editor-row' }, [el('label', { text: F.label }), range, valBox]);
  });

  body.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'Parameters' }),
    el('p', { class: 'hint' }, [
      document.createTextNode('Editing '),
      el('b', { text: `${stage.id} ${stage.name}` }),
      document.createTextNode('. Click any stage on the line above to edit a different one.')
    ]),
    ...rows,
    el('div', { class: 'patchbar' }, S.patches.length ? S.patches.map(p =>
      el('span', { class: 'patch' }, [
        el('span', { text: describePatch(p) }),
        el('button', { text: '×', title: 'Remove this change',
                       onclick: () => { S.patches = S.patches.filter(x => x !== p); renderTab(); } })
      ])) : [el('span', { class: 'empty', text: 'No changes yet. Move a slider or pick a scenario above.' })]),
    el('div', { style: 'display:flex;gap:8px' }, [
      el('button', { class: 'btn primary', text: 'Run scenario', disabled: !S.patches.length, onclick: runWhatIf }),
      el('button', { class: 'btn', text: 'Clear', disabled: !S.patches.length,
                     onclick: () => { S.patches = []; S.whatif = null; S.compareFrames = null; renderTab(); } })
    ])
  ]));

  if (S.whatifBusy) {
    body.appendChild(el('div', { class: 'panel' }, [
      el('span', { class: 'spinner' }),
      el('span', { style: 'margin-left:10px;color:var(--ink-2)',
                   text: `Running ${S.baseline.replications.reps} paired replications…` })
    ]));
    return;
  }
  if (S.whatifError) { body.appendChild(errorBox('The scenario did not run.', S.whatifError)); return; }
  if (S.whatif) body.appendChild(whatIfResult(S.whatif));
}

function patchedValue(cur, p) {
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
};

function describePatch(p) {
  const F = FIELDS.find(f => f.f === p.field);
  const label = F ? F.label : p.field;
  const v = p.op === 'mul' ? `×${p.value.toFixed(2)}` : p.op === 'add' ? `${sgn(p.value, 0)}` : `= ${n2(p.value)}`;
  return `${p.stage} ${label} ${v}`;
}

function setPatch(stageId, field, value, current) {
  S.patches = S.patches.filter(p => !(p.stage === stageId && p.field === field));
  if (Math.abs(value - current) > 1e-9) S.patches.push({ stage: stageId, field, op: 'set', value });
  renderTab();
}

async function runWhatIf() {
  if (!S.patches.length) return;
  S.whatifBusy = true; S.whatifError = null; renderTab();
  try {
    S.whatif = await api(`/api/session/${S.session}/whatif`, {
      method: 'POST',
      body: JSON.stringify({ patches: S.patches, reps: S.baseline.replications.reps })
    });
    S.compareFrames = decodeFrames(S.whatif.scenario_frames);
  } catch (e) {
    S.whatifError = e; S.whatif = null;
  } finally {
    S.whatifBusy = false; renderTab();
  }
}

const METRIC_LABEL = {
  throughput_per_h: 'Throughput (units/h)', wip: 'Work in progress (units)',
  cycle_time_s: 'Cycle time (s)', cycle_time_p95_s: 'Cycle time, 95th pct (s)',
  first_pass_yield: 'First-pass yield', flow_ratio: 'Flow ratio'
};

function whatIfResult(w) {
  const wrap = el('div', {});
  const th = w.deltas.throughput_per_h;

  if (w.bottleneck_migrated) {
    wrap.appendChild(el('div', { class: 'banner' }, [
      el('div', { class: 'mark', text: '→' }),
      el('div', {}, [
        el('b', { text: `The constraint moved from ${w.bottleneck_before} to ${w.bottleneck_after}.` }),
        el('p', { text: 'Fixing a bottleneck creates the next one. This is where the following investment goes.' })
      ])
    ]));
  } else if (!th.significant) {
    wrap.appendChild(el('div', { class: 'banner' }, [
      el('div', { class: 'mark', text: '=' }),
      el('div', {}, [
        el('b', { text: 'No measurable effect.' }),
        el('p', { text: `The confidence interval spans zero, so this change cannot be distinguished from run-to-run noise. The constraint is still ${w.bottleneck_before}.` })
      ])
    ]));
  }

  wrap.appendChild(el('div', { class: 'panel flush' }, [
    el('div', { style: 'padding:16px 16px 0' }, [
      el('h3', { text: 'Before and after' }),
      el('p', { class: 'hint', text: `${w.reps} paired replications, common random numbers. ` +
        'A result is only credited when its 95% interval excludes zero.' })
    ]),
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Metric' }), el('th', { class: 'num', text: 'Baseline' }),
        el('th', { class: 'num', text: 'Scenario' }), el('th', { class: 'num', text: 'Change' }),
        el('th', { class: 'num', text: '95% interval' }), el('th', { text: '' })
      ])]),
      el('tbody', {}, Object.entries(w.deltas).map(([m, d]) => el('tr', { class: d.significant ? '' : 'muted' }, [
        el('td', { text: METRIC_LABEL[m] || m }),
        el('td', { class: 'num', text: n2(d.base) }),
        el('td', { class: 'num', text: n2(d.scenario) }),
        el('td', { class: 'num ' + deltaClass(m, d.delta) , text: `${sgn(d.delta)} (${sgn(d.pct, 1)}%)` }),
        el('td', { class: 'num', text: `${sgn(d.ci_low)} … ${sgn(d.ci_high)}` }),
        el('td', {}, [el('span', { class: 'pill ' + (d.significant ? 'sig' : 'noise'),
                                   text: d.significant ? 'significant' : 'within noise' })])
      ])))
    ])
  ]));

  wrap.appendChild(el('div', { class: 'panel flush' }, [
    el('div', { style: 'padding:16px 16px 0' }, [
      el('h3', { text: 'How each stage responded' }),
      el('p', { class: 'hint', text: 'Where the pressure moved to.' })
    ]),
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Stage' }), el('th', { class: 'num', text: 'Producing' }),
        el('th', { class: 'num', text: 'Blocked' }), el('th', { class: 'num', text: 'Waiting' }),
        el('th', { class: 'num', text: 'Bottleneck share' })
      ])]),
      el('tbody', {}, w.stage_shift.map(s => el('tr', {}, [
        el('td', { text: s.id }),
        shiftCell(s.util), shiftCell(s.blocked), shiftCell(s.starved), shiftCell(s.bn_share)
      ])))
    ])
  ]));

  wrap.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'In plain English' }),
    el('div', { class: 'narration', text: w.narration }),
    el('button', { class: 'btn', text: 'Make this the new baseline',
                   onclick: () => applyPatches(w.patches, 'estimated') })
  ]));
  return wrap;
}

function deltaClass(metric, d) {
  const better = { throughput_per_h: 1, first_pass_yield: 1, wip: -1, cycle_time_s: -1, cycle_time_p95_s: -1, flow_ratio: -1 };
  const dir = better[metric] || 0;
  if (!dir || Math.abs(d) < 1e-9) return '';
  return (d * dir > 0) ? 'pos' : 'neg';
}

function shiftCell(d) {
  return el('td', { class: 'num' }, [
    el('span', { style: 'color:var(--ink-3)', text: pct(d.base) }),
    document.createTextNode(' → '),
    el('span', { text: pct(d.scenario) })
  ]);
}

/* ----------------------------------------------------------- Advisor ---- */
async function pollAdvisor() {
  if (!S.session) return;
  try {
    const r = await api(`/api/session/${S.session}/advisor`);
    if (r.status === 'done') {
      S.advisor = r;
      $('advisorBadge').textContent = String(r.ranked.length);
      if (S.tab === 'advisor') renderTab();
      return;
    }
    if (r.status === 'error') {
      S.advisorError = r.error;
      $('advisorBadge').textContent = '!';
      if (S.tab === 'advisor') renderTab();
      return;
    }
  } catch (e) { /* session may have been replaced; stop quietly */ return; }
  setTimeout(pollAdvisor, 1200);
}

function tabAdvisor(body) {
  body.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'Every single-lever change, searched and ranked' }),
    el('p', { class: 'hint', text:
      'The twin perturbs every stage in turn — faster cycle, extra machine, shorter repair, ' +
      'more buffer — runs paired replications for each, and keeps only the changes whose ' +
      'gain is statistically real. Nobody has to guess which knob to turn.' })
  ]));

  if (S.advisorError) { body.appendChild(errorBox('The intervention search failed.', S.advisorError)); return; }
  if (!S.advisor) {
    body.appendChild(el('div', { class: 'empty-state' }, [
      el('b', {}, [el('span', { class: 'spinner' }), document.createTextNode('  Searching interventions')]),
      el('span', { text: 'Around twenty scenarios, each with paired replications. Takes a few seconds.' })
    ]));
    return;
  }
  const rows = S.advisor.ranked;
  if (!rows.length) {
    body.appendChild(el('div', { class: 'empty-state' }, [
      el('b', { text: 'No single change moves this line' }),
      el('span', { text: 'Every one-lever intervention came back inside run-to-run noise. The line is balanced; the next move is a multi-stage change.' })
    ]));
    return;
  }

  body.appendChild(el('div', { class: 'panel flush' }, [
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '' }), el('th', { text: 'Stage' }), el('th', { text: 'Change' }),
        el('th', { class: 'num', text: 'Throughput gain' }), el('th', { class: 'num', text: '95% interval' }),
        el('th', { class: 'num', text: 'Capital' }), el('th', { class: 'num', text: 'Units/h per 100k' }),
        el('th', { text: '' })
      ])]),
      el('tbody', {}, rows.map((r, i) => el('tr', {}, [
        el('td', { style: 'color:var(--ink-3)', text: String(i + 1) }),
        el('td', { text: r.stage }),
        el('td', { text: r.label }),
        el('td', { class: 'num pos', text: `${sgn(r.delta_th)} (${sgn(r.pct, 1)}%)` }),
        el('td', { class: 'num', text: `${sgn(r.ci_low)} … ${sgn(r.ci_high)}` }),
        el('td', { class: 'num', text: r.cost > 0 ? money(r.cost) : 'no capital' }),
        el('td', { class: 'num', text: r.cost > 0 ? n2(r.delta_th / r.cost * 1e5) : '—' }),
        el('td', {}, [el('button', { class: 'btn small', text: 'Try it',
          onclick: () => { S.patches = leverPatches(r); S.tab = 'whatif'; syncTabs(); renderTab(); runWhatIf(); } })])
      ])))
    ])
  ]));

  body.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'What to do first' }),
    el('div', { class: 'narration', text: S.advisor.narration })
  ]));
}

function leverPatches(r) {
  const L = r.label.toLowerCase();
  if (L.includes('machine')) return [{ stage: r.stage, field: 'machines', op: 'add', value: 1 }];
  if (L.includes('mttr')) return [{ stage: r.stage, field: 'repair.mean', op: 'mul', value: 0.5 }];
  if (L.includes('buffer')) return [{ stage: r.stage, field: 'in_buffer', op: 'add', value: 5 }];
  return [{ stage: r.stage, field: 'proc.mean', op: 'mul', value: 1 / 1.2 }];
}

/* --------------------------------------------------------- Calibrate ---- */
function tabCalibrate(body) {
  body.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'Measure the twin against a real shift' }),
    el('p', { class: 'hint', text:
      'A simulation becomes a twin when it has a measured error against the real line. ' +
      'Upload one shift — per-stage busy and downtime fractions, plus total good units — ' +
      'and the twin reports how wrong it is, then fits itself to match.' }),
    el('p', { class: 'hint', text:
      'Busy fraction alone cannot identify cycle times: scale every processing time by the ' +
      'same factor and throughput scales inversely, leaving utilisation unchanged. So ' +
      'utilisation fixes the ratios between stages and throughput fixes the absolute scale. ' +
      'The fit alternates the two.' }),
    el('button', { class: 'btn primary', text: 'Calibrate against data/observed_shift.csv',
                   disabled: !!S.calibBusy, onclick: runCalibration })
  ]));

  if (S.calibBusy) {
    body.appendChild(el('div', { class: 'empty-state' }, [
      el('b', {}, [el('span', { class: 'spinner' }), document.createTextNode('  Fitting the twin')]),
      el('span', { text: 'Each iteration re-simulates the whole line several times.' })
    ]));
    return;
  }
  if (S.calibError) { body.appendChild(errorBox('Calibration failed.', S.calibError)); return; }
  if (!S.calibration) return;

  const c = S.calibration, before = c.before, after = c.after;
  body.appendChild(el('div', { class: 'kpis' }, [
    kpi('Observed', n1(before.throughput_obs), 'units/h', 'from the shift log'),
    kpi('Twin before', n1(before.throughput_sim), 'units/h', `${n1(before.throughput_err_pct)}% error · ${before.grade}`),
    kpi('Twin after', n1(after.throughput_sim), 'units/h', `${n1(after.throughput_err_pct)}% error · ${after.grade}`),
    kpi('Worst stage error', n1(after.worst_stage_err_pct), '%', `was ${n1(before.worst_stage_err_pct)}% · ${c.iterations} iterations`)
  ]));

  body.appendChild(el('div', { class: 'grid2' }, [
    el('div', { class: 'panel' }, [
      el('h3', { text: 'Convergence' }),
      el('p', { class: 'hint', text: c.method }),
      sparkline(c.trajectory.map(t => ({ x: 'i' + t.iter, y: t.throughput_err_pct })), { color: '#5B8DB8' }),
      el('p', { class: 'hint', style: 'margin:8px 0 0', text: 'Throughput error per iteration, in percent.' })
    ]),
    el('div', { class: 'panel flush' }, [
      el('div', { style: 'padding:16px 16px 0' }, [el('h3', { text: 'Per-stage fit' })]),
      el('table', {}, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: 'Stage' }), el('th', { class: 'num', text: 'Observed busy' }),
          el('th', { class: 'num', text: 'Twin busy' }), el('th', { class: 'num', text: 'Error' })
        ])]),
        el('tbody', {}, after.stages.map(s => el('tr', {}, [
          el('td', { text: `${s.id} ${clip(s.name, 18)}` }),
          el('td', { class: 'num', text: pct(s.busy_obs, 1) }),
          el('td', { class: 'num', text: pct(s.busy_sim, 1) }),
          el('td', { class: 'num', text: n1(s.busy_err_pct) + '%' })
        ])))
      ])
    ])
  ]));

  body.appendChild(el('div', { class: 'panel flush' }, [
    el('div', { style: 'padding:16px 16px 0' }, [
      el('h3', { text: 'Fitted parameters' }),
      el('p', { class: 'hint', text: 'Accepting these marks the fitted fields as measured rather than estimated.' })
    ]),
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Stage' }), el('th', { text: 'Parameter' }),
        el('th', { class: 'num', text: 'Archetype said' }), el('th', { class: 'num', text: 'Your line runs' }),
        el('th', { class: 'num', text: 'Off by' })
      ])]),
      el('tbody', {}, c.patches.map(p => el('tr', {}, [
        el('td', { text: p.stage }), el('td', { text: 'Cycle time' }),
        el('td', { class: 'num', text: n1(p.was) + ' s' }),
        el('td', { class: 'num', text: n2(p.value) + ' s' }),
        el('td', { class: 'num', text: sgn((p.value - p.was) / p.was * 100, 1) + '%' })
      ])))
    ]),
    el('div', { style: 'padding:14px 16px' }, [
      el('button', { class: 'btn primary', text: 'Accept the fit',
                     onclick: () => applyPatches(c.patches.map(p => ({ stage: p.stage, field: 'proc.mean', op: 'set', value: p.value })), 'measured') })
    ])
  ]));
}

async function runCalibration() {
  S.calibBusy = true; S.calibError = null; renderTab();
  try {
    await api(`/api/session/${S.session}/calibrate`, {
      method: 'POST', body: JSON.stringify({ observation: 'observed_shift.csv' })
    });
    for (let i = 0; i < 240; i++) {
      await new Promise(r => setTimeout(r, 1000));
      const j = await api(`/api/session/${S.session}/job?name=calibrate`);
      if (j.status === 'done') { S.calibration = j.result; break; }
      if (j.status === 'error') throw new Error(j.error);
    }
    if (!S.calibration) throw new Error('Calibration timed out after four minutes.');
  } catch (e) {
    S.calibError = e;
  } finally {
    S.calibBusy = false; renderTab();
  }
}

/* -------------------------------------------------------- Line setup ---- */
function tabLineSetup(body) {
  const prov = S.provenance || {};
  body.appendChild(el('div', { class: 'panel' }, [
    el('h3', { text: 'Where every number came from' }),
    el('p', { class: 'hint', text: S.provSummary ? S.provSummary.verdict : '' }),
    el('div', { style: 'display:flex;gap:18px;font-size:12.5px;color:var(--ink-2)' }, [
      el('span', {}, [el('i', { class: 'prov measured' }), document.createTextNode('Measured from your data')]),
      el('span', {}, [el('i', { class: 'prov reference' }), document.createTextNode('Industry benchmark')]),
      el('span', {}, [el('i', { class: 'prov estimated' }), document.createTextNode('Estimated for this line')])
    ])
  ]));

  body.appendChild(el('div', { class: 'panel flush' }, [
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Stage' }), el('th', { class: 'num', text: 'Machines' }),
        el('th', { class: 'num', text: 'Cycle time' }), el('th', { class: 'num', text: 'CV' }),
        el('th', { class: 'num', text: 'Buffer' }), el('th', { class: 'num', text: 'MTBF' }),
        el('th', { class: 'num', text: 'Repair' }), el('th', { class: 'num', text: 'Yield' }),
        el('th', { class: 'num', text: 'Capacity' })
      ])]),
      el('tbody', {}, S.plant.stages.map((s, i) => {
        const cap = S.baseline.report.losses.capacity_table[i];
        const dot = f => el('i', { class: 'prov ' + (prov[`${s.id}:${f}`] || 'reference'),
                                   title: prov[`${s.id}:${f}`] || 'reference' });
        return el('tr', { class: 'clickable', onclick: () => selectStage(s.id) }, [
          el('td', { text: `${s.id} ${s.name}` }),
          el('td', { class: 'num' }, [dot('machines'), document.createTextNode(String(s.machines))]),
          el('td', { class: 'num' }, [dot('proc.mean'), document.createTextNode(n1(s.proc.mean) + ' s')]),
          el('td', { class: 'num', text: n2(s.proc.cv) }),
          el('td', { class: 'num' }, [dot('in_buffer'), document.createTextNode(s.in_buffer === null || s.in_buffer < 0 ? 'unlimited' : String(s.in_buffer))]),
          el('td', { class: 'num' }, [dot('mtbf'), document.createTextNode(s.mtbf === null || s.mtbf < 0 ? 'no failures' : n0(s.mtbf / 60) + ' min')]),
          el('td', { class: 'num', text: s.repair.mean ? n0(s.repair.mean / 60) + ' min' : '—' }),
          el('td', { class: 'num', text: pct(s.yield, 1) }),
          el('td', { class: 'num', text: cap ? n1(cap.r_good) + '/h' : '—' })
        ]);
      }))
    ])
  ]));

  if (S.history && S.history.length) {
    body.appendChild(el('div', { class: 'panel' }, [
      el('h3', { text: 'Changes made in this session' }),
      el('ul', { style: 'margin:0;padding-left:18px;color:var(--ink-2);font-size:12.5px' },
        S.history.map(h => el('li', { text: `${h.action}: ${h.detail}` })))
    ]));
  }
}

async function applyPatches(patches, tier) {
  try {
    const r = await api(`/api/session/${S.session}/apply`, {
      method: 'POST', body: JSON.stringify({ patches, tier })
    });
    S.plant = r.plant; S.provenance = r.provenance; S.provSummary = r.provenance_summary;
    S.history = r.history;
    S.patches = []; S.whatif = null; S.advisor = null; S.calibration = null;
    $('advisorBadge').textContent = '…';
    toast('Baseline updated. Re-simulating.');
    renderProvChip();
    await loadBaseline();
    pollAdvisor();
  } catch (e) {
    toast('Could not update the baseline: ' + e.message, 4200);
  }
}

/* ====================================================================
   Right rail
   ==================================================================== */
function renderRail() {
  const body = $('railBody');
  body.replaceChildren();
  if (!S.baseline) return;
  const r = S.baseline.report;

  body.appendChild(el('div', { class: 'narration' }, [
    document.createTextNode(S.baseline.narration.bottleneck),
    el('span', { class: 'src', text: 'Written from the computed results. Every figure here exists in the data.' })
  ]));

  const stage = r.kpis.stages.find(s => s.id === S.selected);
  if (stage) {
    const p = r.propagation.stages.find(x => x.id === stage.id);
    const plantStage = S.plant.stages.find(x => x.id === stage.id);
    body.appendChild(el('div', { class: 'panel', style: 'padding:13px' }, [
      el('h3', { text: `${stage.id} ${stage.name}` }),
      el('div', { class: 'statebar' }, STATE_KEYS.map(k =>
        el('i', { style: `width:${Math.max(0, stage[k]) * 100}%;background:${STATE_COLOR[k]}`, title: `${pct(stage[k], 1)} ${STATE_WORD[k]}` }))),
      el('dl', { class: 'kv' }, [
        row('Producing', pct(stage.busy, 1)),
        row('Blocked by next stage', pct(stage.blocked, 1)),
        row('Waiting on previous', pct(stage.starved, 1)),
        row('Broken down', pct(stage.down, 1)),
        stage.setup > 0 ? row('In changeover', pct(stage.setup, 1)) : null,
        row('Bottleneck share', pct(stage.bn_share, 1)),
        row('Breakdowns', String(stage.failures)),
        row('Scrapped', String(stage.scrap)),
        row('Input buffer', stage.buf_cap === null ? 'unlimited' : `${n1(stage.buf_avg)} of ${stage.buf_cap}`),
        p ? row('Downtime amplification', n2(p.amplification) + '×') : null,
        plantStage ? row('Cycle time', n1(plantStage.proc.mean) + ' s') : null
      ].filter(Boolean)),
      el('button', {
        class: 'btn small', style: 'margin-top:12px',
        text: 'Explain this stage',
        onclick: () => explainStage(stage.id)
      })
    ]));
  }

  body.appendChild(el('div', { style: 'margin:14px 0 8px;color:var(--ink-3);font-size:11.5px',
                               text: `${r.inefficiencies.length} findings` }));
  for (const d of r.inefficiencies) {
    body.appendChild(el('div', { class: 'insight ' + d.sev, onclick: () => d.stage !== '-' && selectStage(d.stage) }, [
      el('div', { class: 'top' }, [
        el('span', { class: 'who', text: d.stage === '-' ? 'Whole line' : d.stage }),
        el('span', { class: 'code', text: d.code.toLowerCase().replace(/_/g, ' ') })
      ]),
      el('div', { class: 'msg', text: d.msg })
    ]));
  }
}

const row = (k, v) => el('div', { style: 'display:contents' }, [el('dt', { text: k }), el('dd', { text: v })]);

async function explainStage(id) {
  try {
    const r = await api(`/api/session/${S.session}/narrate?mode=stage&stage=${encodeURIComponent(id)}`);
    showDialog(`${id} — what is happening`, el('div', {}, [
      el('p', { text: r.text }),
      r.rejected && r.rejected.length
        ? el('p', { style: 'color:var(--st-setup)', text: `Narration guard rejected invented figures: ${r.rejected.join(', ')}. Showing the computed version instead.` })
        : el('p', { style: 'color:var(--ink-3);font-size:11.5px', text: `Source: ${r.source}. Every figure was checked against the simulation output before display.` })
    ]));
  } catch (e) { toast('Could not generate an explanation: ' + e.message); }
}

function selectStage(id) {
  S.selected = id;
  renderRail();
  if (S.tab === 'whatif') renderTab();
}

/* ====================================================================
   Dialog + wiring
   ==================================================================== */
function showDialog(title, node) {
  $('dlgTitle').textContent = title;
  $('dlgBody').replaceChildren(node);
  $('dlg').showModal();
}

function syncTabs() {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === S.tab));
}

function wire() {
  $('btnDescribe').addEventListener('click', describeCompany);
  $('describeInput').addEventListener('keydown', e => { if (e.key === 'Enter') describeCompany(); });
  $('btnChangeLine').addEventListener('click', () => {
    if (S.timer) { clearInterval(S.timer); S.timer = null; S.playing = false; }
    $('workspace').hidden = true; $('launch').hidden = false;
    $('btnChangeLine').hidden = true; $('repChip').hidden = true;
    $('validChip').hidden = true; $('provChip').hidden = true;
    $('lineName').textContent = '';
    $('matches').className = 'matches';
  });
  $('tabs').addEventListener('click', e => {
    const b = e.target.closest('.tab');
    if (!b) return;
    S.tab = b.dataset.tab; syncTabs(); renderTab();
  });
  $('btnPlay').addEventListener('click', togglePlay);
  $('btnExpandLine').addEventListener('click', toggleLineExpanded);
  $('scrub').addEventListener('input', e => {
    if (S.playing) togglePlay();
    S.frameIdx = Number(e.target.value); paintFrame(S.frameIdx);
  });
  $('speed').addEventListener('change', () => { if (S.playing) { togglePlay(); togglePlay(); } });
  $('dlgClose').addEventListener('click', () => $('dlg').close());
  document.addEventListener('keydown', e => {
    if (e.key === ' ' && S.frames && !$('workspace').hidden &&
        !['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      e.preventDefault(); togglePlay();
    }
  });
}

document.addEventListener('DOMContentLoaded', () => { wire(); bootLaunch(); });

/* exported for the node-based unit tests (tests/test_ui.js) */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { decodeFrames, patchedValue, describePatch, deltaClass,
                     shortBucket, clip, hhmm, pct, sgn, leverPatches,
                     missing, money, n1, n2, fmtField, FIELDS, UNLIMITED,
                     PRESETS, slackestStage, __state: S };
}
