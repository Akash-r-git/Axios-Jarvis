/* ======================================================================
   Live view.

   Renders what the live twin actually reports. Every label here is driven by
   the server's connection badge: if the source is a replay the badge says
   REPLAY, and nothing in this file can override that.
   ====================================================================== */

const LIVE = {
  running: false,
  snap: null,
  es: null,
  timer: null,
  feed: [],
  analysis: null,
};

const LIVE_STATE_COLOR = {
  BUSY: 'var(--st-busy)', SETUP: 'var(--st-setup)', DOWN: 'var(--st-down)',
  BLOCKED: 'var(--st-blocked)', STARVED: 'var(--st-starved)',
  IDLE: 'var(--st-starved)',
};

function liveBadge(c) {
  if (!c) return el('span', { class: 'chip' }, ['no source']);
  const bits = [];
  const stateWord = { connected: 'CONNECTED', connecting: 'CONNECTING',
                      disconnected: 'DISCONNECTED', error: 'ERROR' }[c.state]
                    || String(c.state || '').toUpperCase();
  bits.push(el('span', { class: 'pill' }, [stateWord]));
  if (c.replay) bits.push(el('span', { class: 'pill', title:
    'Recorded or generated session played back through the connector interface. Not measured live data.' }, ['REPLAY']));
  else if (c.live) bits.push(el('span', { class: 'pill' }, ['LIVE']));
  bits.push(el('span', { class: 'chip' }, [c.source || c.label || '—']));
  bits.push(el('span', { class: 'chip' }, [
    'last update ' + (c.last_update
      ? new Date(c.last_update * 1000).toLocaleTimeString() : '—')]));
  if (c.error) bits.push(el('span', { class: 'chip neg' }, [c.error]));
  return el('div', { class: 'statebar' }, bits);
}

function liveMachineGrid(stages) {
  return el('div', { class: 'cards' }, stages.map(st => {
    const cells = st.machines.map(m => el('div', {
      class: 'kpi', title: `${m.machine}: ${m.state || 'no data'} (${m.provenance || '—'})`,
      style: `border-left:4px solid ${LIVE_STATE_COLOR[m.state] || 'var(--line,#444)'}`,
    }, [
      el('div', { class: 'kv' }, [m.machine]),
      el('div', {}, [m.state || 'no data yet']),
      el('div', { class: 'prov' }, [m.state ? hhmm(m.for_s) : '—']),
    ]));
    const buf = st.buffer && st.buffer.level !== null && st.buffer.level !== undefined
      ? `buffer ${n0(st.buffer.level)}/${n0(st.buffer.capacity)}` : null;
    return el('div', { class: 'card' }, [
      el('div', { class: 'section-head' }, [
        el('strong', {}, [st.name || st.stage]),
        buf ? el('span', { class: 'chip' }, [buf]) : null,
      ].filter(Boolean)),
      el('div', { class: 'kpis' }, cells),
    ]);
  }));
}

function livePropagation(snap) {
  const chains = snap.propagation || [];
  if (!chains.length) {
    return el('div', { class: 'insight' }, ['No stage is currently down.']);
  }
  return el('div', {}, chains.map(c => {
    const parts = [
      el('span', { class: 'pill neg' }, [`${c.cause_stage} DOWN`]),
      el('span', { class: 'prov' }, ['observed']),
    ];
    c.effects.forEach(e => {
      parts.push(el('span', { class: 'chip' }, ['→']));
      parts.push(el('span', { class: 'pill' }, [`${e.stage} ${e.effect}`]));
      parts.push(el('span', {
        class: 'prov', title: e.note,
      }, [e.provenance]));
    });
    return el('div', { class: 'insight' }, [
      el('div', { class: 'statebar' }, parts),
      el('div', { class: 'prov' }, [
        `${c.cause_machines.join(', ')} down for ${hhmm(c.down_for_s)}` +
        (c.stage_fully_down ? ' — whole stage stopped' : ' — stage partially running'),
      ]),
    ]);
  }));
}

function liveFeed() {
  const rows = LIVE.feed.slice(-40).reverse().map(e => el('div', { class: 'kv' }, [
    el('span', { class: 'prov' }, [new Date(e.ts * 1000).toLocaleTimeString()]),
    el('span', {}, [` ${e.stage}/${e.machine} `]),
    el('span', { class: 'pill' }, [
      (e.previous_state ? e.previous_state + ' → ' : '') +
      (e.new_state || e.event_type)]),
    el('span', { class: 'prov' }, [' ' + e.provenance]),
  ]));
  return el('div', { class: 'panel' }, rows.length ? rows
    : [el('div', { class: 'empty-state' }, ['waiting for events…'])]);
}

function liveAnomalies(snap) {
  const an = (snap.anomalies || []).slice(-12).reverse();
  if (!an.length) return el('div', { class: 'insight' }, ['No anomalies detected against the baseline.']);
  return el('div', {}, an.map(a => el('div', { class: 'insight' }, [
    el('div', { class: 'statebar' }, [
      el('span', { class: 'pill neg' }, [a.severity || 'info']),
      el('strong', {}, [a.kind]),
      el('span', { class: 'chip' }, [`${a.stage}${a.machine ? '/' + a.machine : ''}`]),
    ]),
    el('div', {}, [a.message]),
    el('div', { class: 'prov' }, [
      `observed ${a.observed} · expected ${a.expected} · ` +
      new Date(a.ts * 1000).toLocaleTimeString()]),
  ])));
}

function tabLive(body) {
  const head = el('div', { class: 'section-head' }, [
    el('strong', {}, ['Live twin']),
    el('div', { class: 'patchbar' }, [
      el('button', { class: 'btn', onclick: () => startLive('replay') },
        [LIVE.running ? 'Restart replay' : 'Start engine replay']),
      el('button', { class: 'btn', onclick: () => startLive('mdfs') },
        ['Connect MDFS']),
      el('button', { class: 'btn', onclick: stopLive, disabled: !LIVE.running },
        ['Stop']),
      el('button', { class: 'btn', onclick: runLiveAnalysis, disabled: !LIVE.running },
        ['Run analysis on current state']),
    ]),
  ]);
  body.appendChild(head);

  if (!LIVE.running || !LIVE.snap) {
    body.appendChild(el('div', { class: 'empty-state' }, [
      'No live session. Start the engine replay to watch state propagate, or ' +
      'connect MDFS (it will report whether it is configured).']));
    return;
  }
  const snap = LIVE.snap;
  body.appendChild(liveBadge(snap.connection));

  (snap.connectors || []).forEach(c => {
    (c.notes || []).forEach(nt => body.appendChild(
      el('div', { class: 'prov' }, [`${c.label}: ${nt}`])));
    if (c.unconfigured_reason) body.appendChild(
      el('div', { class: 'errorbox' }, [c.unconfigured_reason]));
  });

  body.appendChild(el('div', { class: 'grid2' }, [
    el('div', { class: 'card' }, [
      el('div', { class: 'section-head' }, [el('strong', {}, ['Downtime propagation'])]),
      livePropagation(snap),
    ]),
    el('div', { class: 'card' }, [
      el('div', { class: 'section-head' }, [el('strong', {}, ['Anomalies vs baseline'])]),
      liveAnomalies(snap),
    ]),
  ]));

  body.appendChild(liveMachineGrid(snap.stages || []));

  body.appendChild(el('div', { class: 'grid2' }, [
    el('div', { class: 'card' }, [
      el('div', { class: 'section-head' }, [
        el('strong', {}, ['Event feed']),
        el('span', { class: 'chip' }, [
          `${n0(snap.counters.events_applied)} applied · ` +
          `${n0(snap.counters.events_rejected)} rejected · ` +
          `ledger ${snap.ledger_size}/${snap.ledger_cap}`]),
      ]),
      liveFeed(),
    ]),
    el('div', { class: 'card' }, [
      el('div', { class: 'section-head' }, [el('strong', {}, ['On-demand analysis'])]),
      LIVE.analysis
        ? el('div', {}, [
            el('div', { class: 'kpis' }, [
              kpi('Throughput', n1(LIVE.analysis.report.kpis.throughput_per_h),
                  'good units/h', 'from estimated parameters'),
              kpi('Bottleneck', LIVE.analysis.report.bottlenecks[0].id || '—',
                  '', 'existing engine, run on demand'),
            ]),
            el('div', { class: 'prov' }, [LIVE.analysis.note]),
          ])
        : el('div', { class: 'empty-state' }, [
            'Simulation never runs per event. Press “Run analysis on current ' +
            'state” to estimate parameters from the observed stream and run ' +
            'the existing engine once.']),
    ]),
  ]));
}

async function startLive(source) {
  try {
    await api(`/api/session/${S.session}/live/start`,
      { method: 'POST', body: { source, speed: 120 } });
    LIVE.running = true;
    LIVE.feed = [];
    openLiveStream();
    await refreshLive();
    renderTab();
  } catch (e) { errorBox('live', e); }
}

async function stopLive() {
  closeLiveStream();
  try { await api(`/api/session/${S.session}/live/stop`, { method: 'POST', body: {} }); }
  catch (e) { /* stopping a dead session is not an error */ }
  LIVE.running = false; LIVE.snap = null; renderTab();
}

function openLiveStream() {
  closeLiveStream();
  if (typeof EventSource === 'undefined') { pollLive(); return; }
  const es = new EventSource(`/api/session/${S.session}/live/stream`);
  es.addEventListener('tick', ev => {
    try {
      const e = JSON.parse(ev.data);
      LIVE.feed.push(e);
      if (LIVE.feed.length > 200) LIVE.feed.splice(0, LIVE.feed.length - 200);
    } catch (_) { /* ignore a malformed frame */ }
  });
  es.onerror = () => { /* the browser reconnects; the badge shows the truth */ };
  LIVE.es = es;
  pollLive();
}

function closeLiveStream() {
  if (LIVE.es) { LIVE.es.close(); LIVE.es = null; }
  if (LIVE.timer) { clearInterval(LIVE.timer); LIVE.timer = null; }
}

function pollLive() {
  if (LIVE.timer) clearInterval(LIVE.timer);
  LIVE.timer = setInterval(async () => {
    if (!LIVE.running) return;
    await refreshLive();
    if (S.tab === 'live') renderTab();
  }, 1500);
}

async function refreshLive() {
  try {
    const snap = await api(`/api/session/${S.session}/live`);
    LIVE.running = !!snap.running;
    LIVE.snap = snap.running ? snap : null;
    if (snap.running && (!LIVE.feed.length) && snap.events) LIVE.feed = snap.events;
  } catch (e) { LIVE.running = false; }
}

async function runLiveAnalysis() {
  try {
    toast('Running the engine on the current live state…');
    LIVE.analysis = await api(`/api/session/${S.session}/live/analyze`,
      { method: 'POST', body: {} });
    renderTab();
  } catch (e) { errorBox('live analysis', e); }
}
