/* PUGMARK · front end
   No framework, no build step, no network beyond this machine. */

'use strict';

const S = { reserve: null, run: null, config: null, tigers: [], sup: false };

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = (n) => (n ?? 0).toLocaleString('en-IN');

async function api(path, opts) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

/* ── the signature ──────────────────────────────────────────────────────
   A tiger's flank stripes are its identity — that is how the matching
   actually works — so the pattern is the identity marker everywhere and
   there are no avatars in this interface.

   Until the pipeline produces real rectified crops these are generated
   deterministically from the identifier, so the same tiger always shows the
   same pattern. Swapping in the real crop is a one-line change and is
   marked in the code so nobody mistakes this for a photograph. */
function stripeRail(id, cls = '') {
  let h = 2166136261;
  for (const ch of String(id)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
  const rand = () => ((h = Math.imul(h ^ (h >>> 15), 2246822507)) >>> 0) / 4294967296;
  const bands = [];
  let y = 0;
  while (y < 100) {
    const gap = 2 + rand() * 5;
    const w = 1.6 + rand() * 4.5;
    y += gap;
    if (y + w > 100) break;
    const skew = (rand() - 0.5) * 2.6;
    bands.push(`<path d="M0 ${y.toFixed(1)} L12 ${(y + skew).toFixed(1)}
      L12 ${(y + skew + w).toFixed(1)} L0 ${(y + w).toFixed(1)} Z"/>`);
    y += w;
  }
  return `<svg class="stripe ${cls}" viewBox="0 0 12 100" preserveAspectRatio="none"
    role="img" aria-label="Flank pattern for ${esc(id)}">
    <rect width="12" height="100" fill="#c98a44"/>
    <g fill="#20180f">${bands.join('')}</g></svg>`;
}

const meter = (v) => `<span class="meter" title="How well cameras covered this
  tiger's range this cycle"><span class="track"><span class="fill${v < 0.6 ? ' low' : ''}"
  style="width:${Math.round(v * 100)}%"></span></span>
  <span class="num">${v.toFixed(2)}</span></span>`;

const table = (cols, rows) =>
  `<thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
   <tbody>${rows.length ? rows.map((r) => `<tr>${r.join('')}</tr>`).join('')
     : `<tr><td colspan="${cols.length}" class="empty">Nothing yet.</td></tr>`}</tbody>`;

/* ── routing ───────────────────────────────────────────────────────────── */
const RENDER = {};

function route() {
  const name = (location.hash.replace('#', '') || 'run');
  $$('.view').forEach((v) => v.classList.toggle('on', v.dataset.view === name));
  $$('.nav a').forEach((a) => {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  RENDER[name]?.().catch((e) => console.error(name, e));
}

/* ── run ───────────────────────────────────────────────────────────────── */
RENDER.run = async () => {
  const runs = await api(`/api/runs?reserve_id=${S.reserve.reserve_id}`);
  if (!runs.length) { $('#runStats').innerHTML = '<div class="empty">No cycles processed yet.</div>'; return; }
  S.run = await api(`/api/runs/${runs[0].run_id}`);
  const c = S.run.counts;

  $('#runTitle').textContent = S.run.cycle_label || S.run.run_id;
  $('#runpill').hidden = false;
  $('#runCycle').textContent = S.run.cycle_label || '—';
  $('#runCount').textContent = `${nf(c.total)} frames`;

  const pct = c.total ? ((c.subject / c.total) * 100).toFixed(1) : '0';
  $('#runStats').innerHTML = [
    ['Frames read', nf(c.total), S.run.root_path],
    ['With a subject', nf(c.subject), `${pct}% of the card`],
    ['Blank', nf(c.quarantined), 'moved to quarantine'],
    ['People', nf(c.person), 'kept out of the tiger record'],
  ].map(([k, v, s], i) => `<div class="card stat${i === 1 ? ' lead' : ''}">
      <div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>
      <div class="sub">${esc(s)}</div></div>`).join('');

  const LABEL = {
    exif: 'Camera EXIF', ocr: 'Read from the timestamp strip',
    filename: 'Parsed from the filename', inferred: 'Inferred from sequence',
    unknown: 'Could not be determined',
  };
  $('#tsTable').innerHTML = table(['Source', 'Frames'],
    S.run.timestamp_sources.map((r) => [
      `<td>${esc(LABEL[r.src] || r.src)}</td>`, `<td class="n">${nf(r.n)}</td>`]));

  const FLAG = {
    exif_missing_read_from_timestamp_band: 'No EXIF — time read from the burned-in strip',
    timestamp_inferred_from_sequence: 'Time inferred from neighbouring frames',
    camera_clock_reset_corrected: 'Camera clock had reset — corrected, original kept',
  };
  $('#flagTable').innerHTML = table(['Flag', 'Frames'],
    Object.entries(S.run.flags).map(([k, n]) => [
      `<td>${esc(FLAG[k] || k)}</td>`, `<td class="n">${nf(n)}</td>`]));

  $('#runsTable').innerHTML = table(['Cycle', 'Started', 'Frames', 'Stage'],
    runs.map((r) => [
      `<td>${esc(r.cycle_label || r.run_id)}</td>`,
      `<td class="n">${esc((r.started_at || '').slice(0, 10))}</td>`,
      `<td class="n">${nf(r.image_count)}</td>`,
      `<td>${esc(r.stage)}</td>`]));

  const a = S.run.alerts || {};
  const hot = (a.act || 0) + (a.watch || 0);
  const t = $('#tallyAlerts');
  t.textContent = hot || '';
  t.classList.toggle('hot', (a.act || 0) > 0);
};

/* ── triage ────────────────────────────────────────────────────────────── */
RENDER.triage = async () => {
  if (!S.run) await RENDER.run();
  const d = await api(`/api/runs/${S.run.run_id}/triage`);
  const s = d.summary;
  const stageA = d.counts.stage_a || 0;
  const stageB = d.counts.stage_b || 0;

  $('#triageStats').innerHTML = [
    ['Frames quarantined', nf(s.quarantined), 'nothing was deleted'],
    ['Disk freed', `${nf(s.mb)} MB`, 'recoverable in full'],
    ['Review time saved', `${nf(s.person_hours_saved)} h`,
      `at ${s.seconds_per_review_assumed}s per frame — an assumption, not a measurement`],
    ['Removed before the detector ran', nf(stageA),
      `${stageB ? Math.round((stageA / (stageA + stageB)) * 100) : 0}% caught by motion alone`],
  ].map(([k, v, sub], i) => `<div class="card stat${i === 2 ? ' lead' : ''}">
      <div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>
      <div class="sub">${esc(sub)}</div></div>`).join('');

  $('#restoreNote').textContent = s.restored
    ? `${nf(s.restored)} frames have already been put back.` : '';

  $('#quarTable').innerHTML = table(['Station', 'Confidence', 'Reason', 'File'],
    d.sample.map((q) => [
      `<td class="n">${esc(q.station_id)}</td>`,
      `<td class="n">${q.conf.toFixed(3)}</td>`,
      `<td>${esc(q.reason)}</td>`,
      `<td class="n" style="color:var(--muted)">${esc(q.orig_path.split('/').pop())}</td>`]));
};

$('#restoreBtn').addEventListener('click', async () => {
  const btn = $('#restoreBtn');
  btn.disabled = true;
  const r = await api(`/api/runs/${S.run.run_id}/quarantine/restore`,
    { method: 'POST', body: { actor: 'director' } });
  $('#restoreNote').textContent =
    `${nf(r.restored)} frames put back at their original paths.`;
  btn.disabled = false;
  RENDER.triage();
});

/* ── tigers ────────────────────────────────────────────────────────────── */
RENDER.tigers = async () => {
  S.tigers = await api(`/api/individuals?reserve_id=${S.reserve.reserve_id}`);
  $('#tallyTigers').textContent = S.tigers.length;
  $('#tigerGrid').innerHTML = S.tigers.map((t) => {
    const sides = (t.sides || '').split(',').filter(Boolean).sort();
    return `<button class="tiger" data-ind="${esc(t.ind_id)}">
      ${stripeRail(t.ind_id)}
      <div>
        <div class="id">${esc(t.ind_id)}</div>
        <div class="meta">${nf(t.station_count)} stations · ${nf(t.crop_count)} confirmed frames</div>
        <div class="sides">
          ${t.provisional ? '<span class="tag prov">Awaiting confirmation</span>' : ''}
          ${sides.map((s) => `<span class="tag">${s === 'L' ? 'Left flank' : 'Right flank'}</span>`).join('')}
        </div>
      </div></button>`;
  }).join('');
};

$('#tigerGrid').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-ind]');
  if (!btn) return;
  const t = await api(`/api/individuals/${btn.dataset.ind}`);
  const seen = t.sides_seen || [];
  const oneSided = seen.length === 1;
  $('#tigerDetail').innerHTML = `
    <div class="hr"></div>
    <div class="card pad">
      <div class="pair">
        ${stripeRail(t.ind_id, 'wide tall')}
        <div style="flex:1">
          <h1>${esc(t.ind_id)}</h1>
          <p class="note">${esc(t.sex || 'sex unrecorded')} ·
             ${esc(t.age_class || 'age unrecorded')} ·
             first seen ${esc((t.first_seen || '').slice(0, 10))}</p>
          ${oneSided ? `<div class="banner" style="margin-top:var(--s4)">
            <b>One flank only</b><span>Only the
            ${seen[0] === 'L' ? 'left' : 'right'} flank has been photographed.
            The two flanks carry different patterns, so a frame of the other
            side cannot be matched to this tiger and will not be treated as a
            new one either.</span></div>` : ''}
        </div>
      </div>
      <div class="hr"></div>
      <h2>Captures</h2>
      <table>${table(['When', 'Station', 'Zone', 'Flank', 'Confidence'],
        t.captures.slice(0, 40).map((c) => [
          `<td class="n">${esc((c.captured_at || '').slice(0, 16).replace('T', ' '))}
             ${c.is_night ? '<span class="tag">night</span>' : ''}</td>`,
          `<td>${esc(c.station_name || c.station_id)}</td>`,
          `<td>${esc(c.zone)}</td>`,
          `<td class="n">${c.side === 'L' ? 'Left' : c.side === 'R' ? 'Right' : '—'}</td>`,
          `<td class="n">${(c.confidence ?? 0).toFixed(2)}</td>`]))}</table>
    </div>`;
  $('#tigerDetail').scrollIntoView({ behavior: 'smooth', block: 'start' });
});

/* ── review ────────────────────────────────────────────────────────────── */
let reviewIdx = 0;
let reviewPick = 0;
let reviewItems = [];

RENDER.review = async () => {
  const d = await api('/api/review?limit=50');
  reviewItems = d.items;
  $('#tallyReview').textContent = d.open || '';
  drawReview();
};

function drawReview() {
  const el = $('#reviewBody');
  const it = reviewItems[reviewIdx];
  if (!it) {
    el.innerHTML = `<div class="card empty"><strong>Queue clear</strong>
      Every match the software was unsure about has been decided.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="review">
      <div class="card pad">
        <h2>Unidentified frame</h2>
        <div class="pair" style="margin-top:var(--s4)">
          ${stripeRail(it.crop_id, 'wide tall')}
          <div style="flex:1">
            <dl class="kv">
              <dt>Flank</dt><dd>${it.side === 'L' ? 'Left' : it.side === 'R' ? 'Right' : 'Unclear'}</dd>
              <dt>Station</dt><dd>${esc(it.station_id)}</dd>
              <dt>Taken</dt><dd>${esc((it.captured_at || '').slice(0, 16).replace('T', ' '))}</dd>
              <dt>Crop quality</dt><dd>${(it.quality ?? 0).toFixed(2)}</dd>
              <dt>Why you are seeing this</dt><dd>${esc(it.reason || 'ambiguous match')}</dd>
            </dl>
            <p class="note">Only ${it.side === 'L' ? 'left' : 'right'}-flank
               candidates are offered. The other flank is a different pattern.</p>
          </div>
        </div>
      </div>
      <div>
        <h2 style="margin-bottom:var(--s3)">Candidates</h2>
        ${it.candidates.map((c, i) => `
          <button class="cand" data-pick="${i}" aria-pressed="${i === reviewPick}">
            ${stripeRail(c.ind_id)}
            <div style="flex:1">
              <div class="k">${esc(c.ind_id)} <kbd>${i + 1}</kbd></div>
              <div class="e">match ${c.score.toFixed(3)} · ${esc(c.evidence)}</div>
            </div></button>`).join('')}
        <button class="cand" data-pick="new" aria-pressed="false">
          <div style="flex:1"><div class="k">Not any of these <kbd>N</kbd></div>
          <div class="e">record as a tiger not yet in the catalogue</div></div></button>
        <div class="toolbar" style="margin-top:var(--s4)">
          <button class="primary" id="confirmBtn">Confirm <kbd>↵</kbd></button>
          <button id="skipBtn">Skip <kbd>J</kbd></button>
        </div>
        <p class="note">${reviewIdx + 1} of ${reviewItems.length} ·
           highest-impact first</p>
      </div>
    </div>`;
}

$('#reviewBody').addEventListener('click', (e) => {
  const pick = e.target.closest('[data-pick]');
  if (pick) {
    reviewPick = pick.dataset.pick === 'new' ? 'new' : Number(pick.dataset.pick);
    $$('#reviewBody .cand').forEach((b) =>
      b.setAttribute('aria-pressed', String(b.dataset.pick === pick.dataset.pick)));
    return;
  }
  if (e.target.closest('#confirmBtn')) confirmReview();
  if (e.target.closest('#skipBtn')) { reviewIdx++; reviewPick = 0; drawReview(); }
});

async function confirmReview() {
  const it = reviewItems[reviewIdx];
  if (!it) return;
  const isNew = reviewPick === 'new';
  const ind = isNew ? it.candidates[0].ind_id : it.candidates[reviewPick].ind_id;
  await api(`/api/review/${it.queue_id}/decide`,
    { method: 'POST', body: { ind_id: ind, actor: 'director', new_individual: isNew } });
  reviewItems.splice(reviewIdx, 1);
  reviewPick = 0;
  if (reviewIdx >= reviewItems.length) reviewIdx = Math.max(0, reviewItems.length - 1);
  $('#tallyReview').textContent = reviewItems.length || '';
  drawReview();
}

/* Keyboard first: nobody doing 200 reviews reaches for a mouse. */
document.addEventListener('keydown', (e) => {
  if (!$('#v-review').classList.contains('on')) return;
  if (e.target.matches('input, textarea')) return;
  const k = e.key.toLowerCase();
  if (k === 'j') { reviewIdx = Math.min(reviewIdx + 1, reviewItems.length - 1); reviewPick = 0; drawReview(); }
  else if (k === 'k') { reviewIdx = Math.max(reviewIdx - 1, 0); reviewPick = 0; drawReview(); }
  else if (k === 'n') { reviewPick = 'new'; drawReview(); }
  else if ('12345'.includes(k)) { reviewPick = Number(k) - 1; drawReview(); }
  else if (e.key === 'Enter') confirmReview();
  else return;
  e.preventDefault();
});

/* ── map ───────────────────────────────────────────────────────────────── */
RENDER.map = async () => {
  if (!S.run) await RENDER.run();
  const [stations, occ] = await Promise.all([
    api(`/api/stations?reserve_id=${S.reserve.reserve_id}`),
    api(`/api/runs/${S.run.run_id}/occupancy`),
  ]);
  const W = 900, H = 520, P = 34;
  const lats = stations.map((s) => s.lat);
  const lons = stations.map((s) => s.lon);
  const [y0, y1] = [Math.min(...lats), Math.max(...lats)];
  const [x0, x1] = [Math.min(...lons), Math.max(...lons)];
  const X = (lon) => P + ((lon - x0) / ((x1 - x0) || 1)) * (W - 2 * P);
  const Y = (lat) => H - P - ((lat - y0) / ((y1 - y0) || 1)) * (H - 2 * P);

  const hulls = occ.filter((o) => o.hull_wkt).map((o) => {
    const pts = o.hull_wkt.replace(/POLYGON\(\(|\)\)/g, '').split(', ')
      .map((p) => p.trim().split(' ').map(Number))
      .map(([lon, lat]) => `${X(lon).toFixed(1)},${Y(lat).toFixed(1)}`).join(' ');
    return `<polygon class="hull" points="${pts}"><title>${esc(o.ind_id)} —
      ${o.area_km2} km²</title></polygon>`;
  }).join('');

  const DEAD = new Set(['PN-C-008', 'PN-C-009']);
  const NEW = new Set(['PN-C-015']);
  const pins = stations.map((s) => {
    const cls = DEAD.has(s.station_id) ? 'dead' : NEW.has(s.station_id) ? 'new' : '';
    return `<circle class="stn ${cls}" cx="${X(s.lon).toFixed(1)}"
      cy="${Y(s.lat).toFixed(1)}" r="${cls ? 5 : 3.4}"><title>${esc(s.name)}
      (${esc(s.zone)}) — ${esc(s.station_id)}</title></circle>`;
  }).join('');

  $('#mapSvg').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Camera stations and tiger home ranges">
    <rect class="zone-buffer" x="8" y="8" width="${W - 16}" height="${H - 16}" rx="4"/>
    <rect class="zone-core" x="${P + 60}" y="${P + 40}" width="${W - 2 * P - 120}"
          height="${H - 2 * P - 80}" rx="4"/>
    <text x="${P + 70}" y="${P + 60}" font-size="11" fill="#6d7566"
          letter-spacing="2" font-family="Bahnschrift, sans-serif">CORE</text>
    <text x="20" y="26" font-size="11" fill="#6d7566" letter-spacing="2"
          font-family="Bahnschrift, sans-serif">BUFFER</text>
    ${hulls}${pins}</svg>`;

  $('#occTable').innerHTML = table(
    ['Tiger', 'Stations', 'Area km²', 'Events', 'Camera-days', 'Note'],
    occ.map((o) => [
      `<td class="n">${esc(o.ind_id)}</td>`,
      `<td class="n">${o.station_set.length}</td>`,
      `<td class="n">${o.area_km2 ?? '—'}</td>`,
      `<td class="n">${nf(o.event_count)}</td>`,
      `<td class="n">${o.effort_days}</td>`,
      `<td style="color:var(--muted)">${esc(o.insufficient_reason || '')}</td>`]));
};

/* ── alerts ────────────────────────────────────────────────────────────── */
const KIND = {
  centroid_shift: 'Range has moved', new_station: 'New camera used',
  buffer_ward: 'Moving toward people', absence: 'Not seen this cycle',
};

RENDER.alerts = async () => {
  if (!S.run) await RENDER.run();
  const d = await api(`/api/runs/${S.run.run_id}/alerts?suppressed=${S.sup}`);
  const c = d.counts;
  $('#alertNote').textContent =
    `${c.act} to act on · ${c.watch} to watch · ${c.info} for information · ${c.suppressed} not raised`;

  $('#alertList').innerHTML = d.items.length ? d.items.map((a) => `
    <article class="alert ${esc(a.severity)}${a.suppressed ? ' suppressed' : ''}">
      ${stripeRail(a.ind_id)}
      <div style="flex:1">
        <div style="display:flex;gap:var(--s3);align-items:baseline;flex-wrap:wrap">
          <span class="who">${esc(a.ind_id)}</span>
          <span class="kind">${esc(KIND[a.type] || a.type)}</span>
          <div style="flex:1"></div>
          ${meter(a.effort_coverage)}
          <span class="num" title="Never higher than the confidence of the
            identification underneath it">conf ${a.confidence.toFixed(2)}</span>
        </div>
        <p class="what">${esc(a.what_changed)}</p>
        ${a.suppressed ? `<div class="why"><b>Not raised</b><br>${esc(a.suppress_reason)}</div>` : ''}
        <div class="evidence">${Object.entries(a.evidence).map(([k, v]) =>
          `<span>${esc(k.replace(/_/g, ' '))}: ${esc(Array.isArray(v) ? v.join(', ') : v)}</span>`).join('')}</div>
      </div>
    </article>`).join('')
    : `<div class="card empty"><strong>${S.sup ? 'Nothing was held back' : 'No alerts'}</strong>
       ${S.sup ? 'Every deviation found this cycle was raised.'
               : 'Nothing changed enough this cycle to need your attention.'}</div>`;
};

$('#tabRaised').addEventListener('click', () => setAlertTab(false));
$('#tabSup').addEventListener('click', () => setAlertTab(true));
function setAlertTab(sup) {
  S.sup = sup;
  $('#tabRaised').setAttribute('aria-pressed', String(!sup));
  $('#tabSup').setAttribute('aria-pressed', String(sup));
  RENDER.alerts();
}

/* ── audit ─────────────────────────────────────────────────────────────── */
RENDER.audit = async () => {
  const q = $('#auditQ').value.trim();
  const rows = await api(`/api/audit?limit=200${q ? `&q=${encodeURIComponent(q)}` : ''}`);
  $('#auditTable').innerHTML = table(['When', 'Who', 'What', 'Entity', 'Detail'],
    rows.map((r) => [
      `<td>${esc((r.ts || '').replace('T', ' ').slice(0, 19))}</td>`,
      `<td>${esc(r.actor)}</td>`,
      `<td>${esc(r.action)}</td>`,
      `<td>${esc(r.entity_id || '')}</td>`,
      `<td style="color:var(--muted)">${esc(r.note || r.after || '')}</td>`]));
};
$('#auditQ').addEventListener('input', () => {
  clearTimeout($('#auditQ')._t);
  $('#auditQ')._t = setTimeout(RENDER.audit, 200);
});

/* ── ops ───────────────────────────────────────────────────────────────── */
RENDER.ops = async () => {
  const d = await api(`/api/ops?reserve_id=${S.reserve.reserve_id}`);
  $('#driftTable').innerHTML = table(
    ['Cycle', 'Frames', 'Blank', 'Mean match score', 'Open reviews'],
    d.drift.map((r) => [
      `<td>${esc(r.cycle_label || r.run_id)}</td>`,
      `<td class="n">${nf(r.images)}</td>`,
      `<td class="n">${nf(r.blanks)}</td>`,
      `<td class="n">${r.mean_auto_conf ?? '—'}</td>`,
      `<td class="n">${nf(r.review_open)}</td>`]));

  const mv = JSON.parse(S.run?.model_versions || '{}');
  $('#verKv').innerHTML = [
    ['Application', d.app_version], ['Database schema', `v${d.schema_version}`],
    ...Object.entries(mv),
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');

  const cfg = S.config;
  $('#cfgKv').innerHTML = [
    ['Auto-accept a match at', cfg.identify.t_high],
    ['Ask a person below', cfg.identify.t_low],
    ['Blank if motion under', cfg.triage.stage_a_blank_threshold],
    ['Core range-shift limit', `${cfg.alerts.core_shift_km_effective} km`],
    ['Buffer range-shift limit', `${cfg.alerts.buffer_shift_km} km`],
    ['Report absence only above', `${cfg.alerts.absence_min_effort_coverage} coverage`],
    ['Burst window', `${cfg.ingest.burst_window_s} s`],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
};

/* ── sync ──────────────────────────────────────────────────────────────── */
RENDER.sync = async () => {
  const d = await api('/api/sync/status');
  $('#syncBody').innerHTML = `
    <div class="card pad">
      <h2>Status</h2>
      <dl class="kv" style="margin-top:var(--s3)">
        <dt>Sharing</dt><dd>${d.enabled ? 'On' : 'Off'}</dd>
        <dt>Reason</dt><dd>${esc(d.reason)}</dd>
        <dt>Last bundle</dt><dd>${esc(d.last_bundle || 'never')}</dd>
      </dl>
      <div class="hr"></div>
      <p>Results leave this machine as a single signed file. It can travel over
         a network when there is one, or on a USB drive when there is not.
         Applying the same file twice changes nothing.</p>
      <div class="toolbar" style="margin-top:var(--s4)">
        <button disabled>Write bundle to drive</button>
        <button disabled>Apply a bundle</button>
        <span class="note">Not available on this node — no central tier configured.</span>
      </div>
    </div>`;
};

/* ── boot ──────────────────────────────────────────────────────────────── */
(async function boot() {
  const [reserves, cfg] = await Promise.all([api('/api/reserves'), api('/api/config')]);
  S.reserve = reserves[0];
  S.config = cfg;
  if (!S.reserve) {
    document.querySelector('main').innerHTML =
      `<div class="card empty"><strong>No reserve set up yet</strong>
       Add a station list to begin. Run <span class="num">python -m tools.seed_demo</span>
       to load the demonstration reserve.</div>`;
    return;
  }
  $('#reserveName').textContent = S.reserve.name;
  await RENDER.run();
  await RENDER.review();
  window.addEventListener('hashchange', route);
  route();
})();
