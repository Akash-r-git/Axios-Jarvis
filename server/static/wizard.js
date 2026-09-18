/* ======================================================================
   "+ Create New Factory" wizard, and the Data Sources view.

   Five steps: name -> source -> discover -> mapping review -> review twin,
   then straight into the existing baseline / what-if / advisor workspace.
   Every number shown in the review came from the deterministic pipeline; the
   only thing an LLM may touch here is a suggested column name, and that is
   marked as a suggestion and still editable.
   ====================================================================== */

const WIZ = {
  open: false, step: 0, name: '', source: null,
  file: null, fileName: '', discovery: null, proposal: null,
  edits: [], llm: null, busy: false, suggestions: null,
};

const WIZ_STEPS = ['Name', 'Source', 'Discover', 'Mapping', 'Review'];

function wizRender() {
  const host = $('wizard');
  if (!host) return;
  host.replaceChildren();
  if (!WIZ.open) return;

  host.appendChild(el('div', { class: 'wiz-steps' },
    WIZ_STEPS.map((s, i) => el('span', { class: 'pill' }, [`${i + 1}. ${s}`]))));

  const body = el('div', { class: 'card' });
  host.appendChild(body);
  ([wizName, wizSource, wizDiscover, wizMapping, wizReview][WIZ.step])(body);
}

/* ---------------------------------------------------------------- step 1 */
function wizName(body) {
  const input = el('input', {
    placeholder: 'e.g. Greenfield Components — Line 2',
    value: WIZ.name, autocomplete: 'off',
  });
  body.appendChild(el('div', { class: 'section-head' },
    [el('strong', {}, ['What is this line called?'])]));
  body.appendChild(el('div', { class: 'describe-row' }, [
    input,
    el('button', {
      class: 'btn primary',
      onclick: () => {
        WIZ.name = input.value.trim() || 'New factory';
        WIZ.step = 1; wizRender();
      },
    }, ['Next']),
  ]));
}

/* ---------------------------------------------------------------- step 2 */
function wizSource(body) {
  body.appendChild(el('div', { class: 'section-head' },
    [el('strong', {}, ['Where does its data come from?'])]));
  const pick = (src) => { WIZ.source = src; WIZ.step = 2; wizRender(); };
  body.appendChild(el('div', { class: 'cards' }, [
    el('button', { class: 'card preset', onclick: () => pick('file') }, [
      el('strong', {}, ['Upload a file']),
      el('div', { class: 'prov' }, ['CSV, JSON or XLSX event log or summary table']),
    ]),
    el('button', { class: 'card preset', onclick: () => pick('describe') }, [
      el('strong', {}, ['Describe with AI']),
      el('div', { class: 'prov' }, [
        WIZ.llm && WIZ.llm.configured
          ? 'Gemini drafts an ESTIMATED model you then calibrate'
          : 'Needs GEMINI_API_KEY — falls back to the archetype matcher']),
    ]),
    el('button', { class: 'card preset', onclick: () => pick('rest') }, [
      el('strong', {}, ['Connect a REST endpoint']),
      el('div', { class: 'prov' }, ['Polls a JSON endpoint for live state changes']),
    ]),
  ]));
  body.appendChild(el('button', {
    class: 'btn small ghost', onclick: () => { WIZ.step = 0; wizRender(); },
  }, ['Back']));
}

/* ---------------------------------------------------------------- step 3 */
function wizDiscover(body) {
  if (WIZ.source === 'describe') return wizDescribe(body);
  if (WIZ.source === 'rest') return wizRest(body);

  body.appendChild(el('div', { class: 'section-head' },
    [el('strong', {}, ['Upload the file'])]));
  const file = el('input', { type: 'file', accept: '.csv,.json,.xlsx,.txt' });
  file.addEventListener('change', () => {
    WIZ.file = file.files[0] || null;
    WIZ.fileName = WIZ.file ? WIZ.file.name : '';
    wizRender();
  });
  body.appendChild(file);
  if (WIZ.fileName) body.appendChild(el('div', { class: 'prov' }, [WIZ.fileName]));
  body.appendChild(el('div', { class: 'patchbar' }, [
    el('button', {
      class: 'btn small ghost', onclick: () => { WIZ.step = 1; wizRender(); },
    }, ['Back']),
    el('button', {
      class: 'btn primary', disabled: !WIZ.file || WIZ.busy,
      onclick: wizRunDiscover,
    }, [WIZ.busy ? 'Reading…' : 'Discover schema']),
  ]));
}

async function wizRunDiscover() {
  if (!WIZ.file) return;
  WIZ.busy = true; wizRender();
  try {
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result).split(',')[1]);
      r.onerror = () => rej(new Error('could not read that file'));
      r.readAsDataURL(WIZ.file);
    });
    WIZ.discovery = await api('/api/ingest/discover', {
      method: 'POST',
      body: { filename: WIZ.fileName, content_b64: b64, name: WIZ.name },
    });
    WIZ.proposal = WIZ.discovery.proposal;
    WIZ.llm = WIZ.discovery.llm;
    WIZ.step = 3;
  } catch (e) {
    $('wizard').prepend(errorBox('Could not read that file.', e));
  } finally { WIZ.busy = false; wizRender(); }
}

function wizDescribe(body) {
  body.appendChild(el('div', { class: 'section-head' },
    [el('strong', {}, ['Describe the line'])]));
  if (WIZ.llm && !WIZ.llm.configured) {
    body.appendChild(el('div', { class: 'errorbox' }, [WIZ.llm.note]));
  }
  const input = el('input', {
    placeholder: 'e.g. blanking, two press lines, welding, paint, final assembly',
    autocomplete: 'off',
  });
  body.appendChild(el('div', { class: 'describe-row' }, [
    input,
    el('button', {
      class: 'btn primary',
      onclick: async () => {
        try {
          const r = await api('/api/ingest/describe', {
            method: 'POST', body: { description: input.value },
          });
          if (!r.session) {
            $('wizard').prepend(el('div', { class: 'errorbox' },
              [r.note || r.error || 'No model was produced.']));
            return;
          }
          toast('ESTIMATED model created — calibrate it before trusting absolute numbers.');
          wizClose();
          enterWorkspace(r);
        } catch (e) { $('wizard').prepend(errorBox('Describe failed.', e)); }
      },
    }, ['Draft an ESTIMATED model']),
  ]));
  body.appendChild(el('div', { class: 'prov' }, [
    'Every value produced this way is tagged ESTIMATED. Nothing here is measured.']));
  body.appendChild(el('button', {
    class: 'btn small ghost', onclick: () => { WIZ.step = 1; wizRender(); },
  }, ['Back']));
}

function wizRest(body) {
  body.appendChild(el('div', { class: 'section-head' },
    [el('strong', {}, ['Connect a REST endpoint'])]));
  body.appendChild(el('div', { class: 'prov' }, [
    'A REST source feeds the live twin; it does not define a line on its own. ' +
    'Create the line from a file or a description first, then add the endpoint ' +
    'in the Data sources tab.']));
  body.appendChild(el('button', {
    class: 'btn small ghost', onclick: () => { WIZ.step = 1; wizRender(); },
  }, ['Back']));
}

/* ---------------------------------------------------------------- step 4 */
function wizQuality(q) {
  const ws = (q && q.warnings) || [];
  if (!ws.length) return el('div', { class: 'insight' }, ['No data-quality warnings.']);
  return el('div', {}, ws.map(w => el('div', { class: 'insight' }, [
    el('span', { class: 'pill' }, [w.kind.replace(/_/g, ' ')]),
    el('span', {}, [' ' + w.detail]),
    w.count ? el('span', { class: 'prov' }, [` (${w.count})`]) : null,
  ].filter(Boolean))));
}

function wizMapping(body) {
  const p = WIZ.proposal;
  const t = WIZ.discovery.table;
  body.appendChild(el('div', { class: 'section-head' }, [
    el('strong', {}, ['Mapping proposal']),
    el('span', { class: 'chip' }, [
      `${t.rows} rows · ${String(t.format).toUpperCase()} · ${t.kind.replace('_', ' ')}`]),
  ]));
  body.appendChild(el('div', { class: 'prov' }, [p.method_reason]));

  if (!p.topology.serial) {
    body.appendChild(el('div', { class: 'errorbox' }, [p.topology.verdict]));
  }
  body.appendChild(wizQuality(p.quality));

  body.appendChild(el('div', { class: 'section-head' }, [el('strong', {}, ['Columns'])]));
  body.appendChild(el('div', { class: 'panel' }, t.columns.map(c =>
    el('div', { class: 'kv' }, [
      el('span', {}, [c.source]),
      el('span', { class: 'pill' }, [c.canonical || 'unmapped']),
      el('span', { class: 'prov' }, [
        ` ${Math.round(c.confidence * 100)}% · ${c.reason}`]),
    ]))));

  if (WIZ.llm && WIZ.llm.configured) {
    body.appendChild(el('button', { class: 'btn small', onclick: wizAskGemini },
      ['Ask Gemini about the unmapped columns']));
  }
  if (WIZ.suggestions) {
    body.appendChild(el('div', { class: 'insight' }, [
      el('div', { class: 'prov' }, [WIZ.suggestions.note || '']),
      ...(WIZ.suggestions.accepted || []).map(a => el('div', { class: 'kv' }, [
        el('span', {}, [`${a.field} → ${a.target}`]),
        el('span', { class: 'prov' }, [
          ` suggestion, ${Math.round(a.confidence * 100)}% · ${a.reason}`]),
      ])),
      ...(WIZ.suggestions.rejected || []).map(r => el('div', { class: 'kv' }, [
        el('span', { class: 'prov' }, [`rejected: ${r.item} — ${r.why}`]),
      ])),
    ]));
  }

  body.appendChild(el('div', { class: 'section-head' }, [
    el('strong', {}, ['Stages — edit anything you disagree with'])]));
  const rows = p.stages.map(s => {
    const num = (field, step) => {
      const i = el('input', {
        type: 'number', step: step || 'any',
        value: (s[field] === null || s[field] === undefined) ? '' : s[field],
        style: 'width:6.5rem',
      });
      i.addEventListener('change', () => {
        const v = i.value === '' ? null : Number(i.value);
        s[field] = v;
        WIZ.edits.push({ id: s.id, [field]: v });
      });
      return i;
    };
    const inc = el('input', { type: 'checkbox', checked: s.include !== false });
    inc.addEventListener('change', () => {
      s.include = inc.checked;
      WIZ.edits.push({ id: s.id, include: inc.checked });
    });
    const nm = el('input', { value: s.name, style: 'width:11rem' });
    nm.addEventListener('change', () => {
      s.name = nm.value;
      WIZ.edits.push({ id: s.id, name: nm.value });
    });
    return el('div', { class: 'editor-row' }, [
      inc, nm,
      el('span', { class: 'chip' }, ['machines']), num('machine_count', '1'),
      el('span', { class: 'chip' }, ['cycle s']), num('proc_mean_s'),
      el('span', { class: 'chip' }, ['CV']), num('proc_cv'),
      el('span', { class: 'pill' }, [`${Math.round(s.confidence * 100)}% confident`]),
      el('span', { class: 'prov' }, [
        ` ${s.reason} · cycle samples ${s.samples.cycle}, repairs ` +
        `${s.samples.repair} · proc ${s.provenance.proc}`]),
    ]);
  });
  body.appendChild(el('div', { class: 'panel' }, rows));

  body.appendChild(el('div', { class: 'patchbar' }, [
    el('button', {
      class: 'btn small ghost', onclick: () => { WIZ.step = 2; wizRender(); },
    }, ['Back']),
    el('button', {
      class: 'btn primary', onclick: () => { WIZ.step = 4; wizRender(); },
    }, ['Review the twin']),
  ]));
}

async function wizAskGemini() {
  const t = WIZ.discovery.table;
  const unmapped = t.columns.filter(c => !c.canonical).map(c => c.source);
  if (!unmapped.length) { toast('Every column is already mapped.'); return; }
  try {
    WIZ.suggestions = await api('/api/ingest/suggest', {
      method: 'POST', body: { headers: unmapped },
    });
  } catch (e) {
    WIZ.suggestions = { note: String(e), accepted: [], rejected: [] };
  }
  wizRender();
}

/* ---------------------------------------------------------------- step 5 */
function wizReview(body) {
  const kept = WIZ.proposal.stages.filter(s => s.include !== false);
  body.appendChild(el('div', { class: 'section-head' }, [
    el('strong', {}, ['Review the twin']),
    el('span', { class: 'chip' }, [`${kept.length} stages in series`]),
  ]));
  body.appendChild(el('div', { class: 'panel' }, kept.map((s, i) =>
    el('div', { class: 'kv' }, [
      el('span', { class: 'pill' }, [String(i + 1)]),
      el('span', {}, [` ${s.name} — ${s.machine_count} machine(s), `]),
      el('span', {}, [`${n1(s.proc_mean_s)}s per part`]),
      el('span', { class: 'prov' }, [` ${s.provenance.proc}`]),
    ]))));
  body.appendChild(el('div', { class: 'prov' }, [
    'Stages run in series; parallel machines within a stage are supported. ' +
    'Branching or merging routes are not, and are flagged rather than flattened.']));
  body.appendChild(el('div', { class: 'patchbar' }, [
    el('button', {
      class: 'btn small ghost', onclick: () => { WIZ.step = 3; wizRender(); },
    }, ['Back']),
    el('button', { class: 'btn primary', disabled: WIZ.busy, onclick: wizLaunch },
      [WIZ.busy ? 'Building…' : 'Launch this twin']),
  ]));
}

async function wizLaunch() {
  WIZ.busy = true; wizRender();
  try {
    const r = await api('/api/ingest/build', {
      method: 'POST',
      body: { proposal: Object.assign({}, WIZ.proposal, { name: WIZ.name }),
              edits: WIZ.edits },
    });
    wizClose();
    enterWorkspace(r);
    if (r.notes && r.notes.length) {
      toast(`${r.notes.length} parameter(s) fell back to reference values.`, 4200);
    }
  } catch (e) {
    $('wizard').prepend(errorBox('Could not build that twin.', e));
  } finally { WIZ.busy = false; }
}

function wizClose() { WIZ.open = false; wizRender(); }

async function wizOpen() {
  WIZ.open = true; WIZ.step = 0; WIZ.edits = []; WIZ.suggestions = null;
  try { WIZ.llm = await api('/api/llm'); } catch (e) { WIZ.llm = null; }
  wizRender();
}

/* ======================================================================
   Data sources view
   ====================================================================== */
function tabSources(body) {
  body.appendChild(el('div', { class: 'section-head' }, [
    el('strong', {}, ['Data sources']),
    el('button', {
      class: 'btn small',
      onclick: async () => { await refreshLive(); renderTab(); },
    }, ['Refresh']),
  ]));
  const cs = (LIVE.snap && LIVE.snap.connectors) || [];
  if (!cs.length) {
    body.appendChild(el('div', { class: 'empty-state' }, [
      'No connectors yet. Start a session from the Live tab.']));
    return;
  }
  body.appendChild(sourceAddForm());
  body.appendChild(el('div', { class: 'panel' }, cs.map(c =>
    el('div', { class: 'insight' }, [
      el('div', { class: 'source-head' }, [
        el('span', { class: 'pill' }, [String(c.state).toUpperCase()]),
        el('strong', {}, [c.label]),
        el('span', { class: 'chip' }, [c.kind]),
        el('span', { class: 'chip' }, [
          c.delivery === 'replay' ? 'REPLAY' : c.delivery]),
      ]),
      el('div', { class: 'prov' }, [
        `${c.source_label} · ${n0(c.events_emitted)} events · last update ` +
        (c.last_update ? new Date(c.last_update * 1000).toLocaleTimeString() : '—') +
        (c.attempts ? ` · ${c.attempts} failed attempt(s)` : '') +
        (c.next_retry_in_s ? ` · retry in ${n0(c.next_retry_in_s)}s` : '')]),
      c.error ? el('div', { class: 'errorbox' }, [c.error]) : null,
      c.unconfigured_reason
        ? el('div', { class: 'errorbox' }, [c.unconfigured_reason]) : null,
      c.mqtt_note ? el('div', { class: 'prov' }, ['MQTT: ' + c.mqtt_note]) : null,
      el('div', { class: 'patchbar' }, [
        el('button', { class: 'btn small', onclick: () => sourceAction(c.id, 'connect') },
          ['Connect']),
        el('button', { class: 'btn small', onclick: () => sourceAction(c.id, 'retry') },
          ['Retry now']),
        el('button', { class: 'btn small ghost', onclick: () => sourceAction(c.id, 'disconnect') },
          ['Disconnect']),
      ]),
    ].filter(Boolean)))));
}

function sourceAddForm() {
  const url = el('input', { placeholder: 'https://mes.example/api/state',
                            style: 'width:18rem' });
  const stage = el('input', { placeholder: 'stage field', style: 'width:8rem' });
  const machine = el('input', { placeholder: 'machine field', style: 'width:8rem' });
  const state = el('input', { placeholder: 'state field', style: 'width:8rem' });
  const ts = el('input', { placeholder: 'timestamp field', style: 'width:9rem' });
  const secs = el('input', { type: 'number', value: 5, min: 1, style: 'width:5rem' });
  return el('div', { class: 'card' }, [
    el('div', { class: 'section-head' }, [el('strong', {}, ['Add a REST source'])]),
    el('div', { class: 'editor-row' }, [
      url, stage, machine, state, ts,
      el('span', { class: 'chip' }, ['poll s']), secs,
      el('button', {
        class: 'btn small primary',
        onclick: async () => {
          if (!url.value.trim()) { toast('A URL is required.'); return; }
          try {
            await api(`/api/session/${S.session}/live/connector`, {
              method: 'POST',
              body: {
                action: 'add_rest', url: url.value.trim(),
                label: url.value.trim(),
                interval: Number(secs.value) || 5,
                field_map: {
                  stage: stage.value.trim() || 'stage',
                  machine: machine.value.trim() || 'machine',
                  state: state.value.trim() || 'state',
                  ts: ts.value.trim() || 'timestamp',
                },
              },
            });
            await refreshLive(); renderTab();
          } catch (e) { errorBox('add source', e); }
        },
      }, ['Add']),
    ]),
    el('div', { class: 'prov' }, [
      'The endpoint must return JSON records. Unknown state values are ' +
      'flagged, never coerced. Credentials are not accepted here — secrets ' +
      'come from environment variables only.']),
  ]);
}

async function sourceAction(id, action) {
  try {
    await api(`/api/session/${S.session}/live/connector`,
      { method: 'POST', body: { id, action } });
    await refreshLive();
    renderTab();
  } catch (e) { errorBox('connector', e); }
}

document.addEventListener('DOMContentLoaded', () => {
  const b = document.getElementById('btnNewFactory');
  if (b) b.addEventListener('click', () => (WIZ.open ? wizClose() : wizOpen()));
});
