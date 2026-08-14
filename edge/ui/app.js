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
  if (!r.ok) {
    let detail = '';
    try { const j = await r.json(); detail = j.detail || j.error || ''; } catch { /* no body */ }
    const err = new Error(`${r.status} ${path}${detail ? `: ${detail}` : ''}`);
    err.detail = detail;
    throw err;
  }
  return r.json();
}

/* ── the signature ──────────────────────────────────────────────────────
   A tiger's flank stripes are its identity — that is how the matching
   actually works — so the pattern is the identity marker everywhere and
   there are no avatars in this interface.

   stripeRail() is the procedural fallback: generated deterministically
   from the identifier, so the same tiger always shows the same pattern,
   for individuals with no real photo on file (every seeded demo tiger --
   the demo has no source images -- and anything not yet identified
   through the real pipeline). flankThumb() is what call sites actually
   use: it requests the real rectified crop from
   /api/individuals/{id}/thumbnail (edge/pipeline/identify_upload.py
   writes these once a real photo has gone through Stage 3) and falls
   back to the procedural pattern on a 404, so nothing here ever claims a
   photograph exists when it does not. */
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

function flankThumb(id, cls = '') {
  return `<span class="stripe-thumb ${cls}">
    <img src="/api/individuals/${encodeURIComponent(id)}/thumbnail" class="real-crop"
         alt="Flank photo for ${esc(id)}" loading="lazy"
         onload="this.classList.add('loaded')" onerror="this.remove()">
    ${stripeRail(id, cls)}
  </span>`;
}

/* Same idea, keyed by crop_id instead of individual -- the review screen
   shows the frame under review before it has been matched to anyone. */
function cropThumb(cropId, fallbackId, cls = '') {
  return `<span class="stripe-thumb ${cls}">
    <img src="/api/crops/${encodeURIComponent(cropId)}/image" class="real-crop"
         alt="Flank photo under review" loading="lazy"
         onload="this.classList.add('loaded')" onerror="this.remove()">
    ${stripeRail(fallbackId, cls)}
  </span>`;
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
  guideOnView(name);
}

/* ── guided walkthrough ───────────────────────────────────────────────────
   A floating, step-by-step coach for someone who did not build this and
   has no reason to already know what each tab means. Steps mirror the
   real workflow order (scan -> confirm -> triage -> check what got
   filtered -> identify a photo -> catalogue -> review queue), not the
   left-nav's visual order, since Identify a photo is what actually
   populates Tigers and has to come first. Advances itself when the
   matching action actually happens (a scan completes, a folder gets
   confirmed, triage finishes, a tab gets opened, a photo gets
   identified) -- Back/Next/Skip always work too, so it never blocks
   anyone who would rather explore on their own terms. */
const GUIDE_STEPS = [
  { id: 'scan', view: 'run', target: '#newRunToggle', title: 'Start here',
    cta: 'Click "Scan a folder" below.',
    body: 'Point it at a folder of camera-trap photos. Nothing is moved or changed until you confirm the next screen.' },
  { id: 'confirm', view: 'run', target: '#nrConfirmBtn', title: 'Confirm the cameras',
    cta: 'Assign each folder to a station, then click Confirm.',
    body: 'Pick a station for each folder, or skip it. This is never guessed for you.' },
  { id: 'triage', view: 'run', target: '#nrTriageBtn', title: 'Run triage',
    cta: 'Click "Run triage".',
    body: 'A fast pass clears the obvious blanks, then the real detector sorts everything left into animal, person, vehicle, or blank.' },
  { id: 'blank', view: 'triage', target: 'a[data-view="triage"]', advanceOnView: true,
    title: 'See what got filtered',
    cta: 'Click "Blank Frames" in the left sidebar.',
    body: 'Everything quarantined as empty shows up there. Nothing is ever deleted -- it can all be put back with one click.' },
  { id: 'identify', view: 'identify', target: 'a[data-view="identify"]', title: 'Identify a tiger',
    cta: 'Click "Identify" next to a frame on the triage results, or open "Identify a photo" in the sidebar.',
    body: 'A bulk scan never matches tigers on its own -- Stage 3 looks at one photo at a time, on purpose. Either path enrols it as a new tiger, matches it to one already known, sends it for review, or refuses -- and always says why.' },
  { id: 'tigers', view: 'tigers', target: 'a[data-view="tigers"]', advanceOnView: true,
    title: 'Your catalogue builds here',
    cta: 'Click "Tigers" in the left sidebar.',
    body: 'Every tiger identified through the previous step appears there with its own real flank photo, not a placeholder.' },
  { id: 'review', view: 'review', target: 'a[data-view="review"]', advanceOnView: true,
    title: 'Uncertain matches',
    cta: 'Click "Needs Review" in the left sidebar.',
    body: 'Anything the matcher was not confident enough to decide alone waits there for a human call.' },
  { id: 'explore', view: null, target: null, title: 'That is the core loop',
    cta: 'Explore the rest whenever you like.',
    body: 'Territories, Alerts, History, System Health and Share Data fill in as you process more cycles. No fixed order after this.' },
];
const GUIDE_KEY = 'pugmark_guide_v1';
S.guide = { open: false, index: 0 };

function guideLoad() {
  try { return JSON.parse(localStorage.getItem(GUIDE_KEY) || 'null'); } catch { return null; }
}
function guideSave() {
  try {
    localStorage.setItem(GUIDE_KEY,
      JSON.stringify({ index: S.guide.index, seen: true }));
  } catch { /* private browsing or storage disabled -- guide still works this visit */ }
}

function guideMark(el, on) {
  if (el) el.classList.toggle('guide-target', on);
}

function guideRender() {
  const panel = $('#guide');
  const prevStep = GUIDE_STEPS[S.guide._lastMarked ?? -1];
  if (prevStep?.target) guideMark(document.querySelector(prevStep.target), false);

  if (!S.guide.open) { panel.hidden = true; return; }
  const step = GUIDE_STEPS[S.guide.index];
  panel.hidden = false;
  $('#guideStepLabel').textContent = `Step ${S.guide.index + 1} of ${GUIDE_STEPS.length}`;
  $('#guideTitle').textContent = step.title;
  $('#guideCta').textContent = step.cta || '';
  $('#guideCta').hidden = !step.cta;
  $('#guideBody').textContent = step.body;
  $('#guideDots').innerHTML = GUIDE_STEPS.map((_, i) =>
    `<span class="dot${i === S.guide.index ? ' on' : ''}"></span>`).join('');
  $('#guideBack').disabled = S.guide.index === 0;
  $('#guideNext').textContent = S.guide.index === GUIDE_STEPS.length - 1 ? 'Done' : 'Next';

  if (step.target) guideMark(document.querySelector(step.target), true);
  S.guide._lastMarked = S.guide.index;
}

function guideGo(index) {
  S.guide.index = Math.max(0, Math.min(GUIDE_STEPS.length - 1, index));
  S.guide.open = true;
  guideSave();
  guideRender();
}

// Called when a real action finishes. Only advances if the guide is
// currently sitting on the step that action satisfies -- otherwise
// someone skipping ahead or working out of order would get yanked
// around by their own actions.
function guideNotify(stepId) {
  const cur = GUIDE_STEPS[S.guide.index];
  if (!cur || cur.id !== stepId) return;
  if (S.guide.index < GUIDE_STEPS.length - 1) guideGo(S.guide.index + 1);
}

function guideOnView(viewName) {
  const cur = GUIDE_STEPS[S.guide.index];
  if (cur && cur.view === viewName && cur.advanceOnView) guideNotify(cur.id);
}

function guideInit() {
  const saved = guideLoad();
  $('#guideBack').onclick = () => guideGo(S.guide.index - 1);
  $('#guideNext').onclick = () => {
    if (S.guide.index === GUIDE_STEPS.length - 1) { S.guide.open = false; guideSave(); guideRender(); return; }
    guideGo(S.guide.index + 1);
  };
  $('#guideClose').onclick = () => { S.guide.open = false; guideSave(); guideRender(); };
  $('#guideReopen').onclick = () => guideGo(saved ? S.guide.index : 0);

  if (!saved) {
    S.guide.index = 0;
    S.guide.open = true;
  } else {
    S.guide.index = saved.index;
    S.guide.open = false;   // seen before -- available via the reopen button, not forced open again
  }
  guideRender();
}

/* ── run ───────────────────────────────────────────────────────────────── */
RENDER.run = async () => {
  const runs = await api(`/api/runs?reserve_id=${S.reserve.reserve_id}`);
  if (!runs.length) { $('#runStats').innerHTML = '<div class="empty">No cycles processed yet.</div>'; return; }
  S.run = await api(`/api/runs/${runs[0].run_id}`);
  const c = S.run.counts;

  $('#runTitle').textContent = S.run.cycle_label || S.run.run_id;
  $('#camtrapdpExport').href = `/api/runs/${S.run.run_id}/export/camtrapdp`;
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

  // A run that stopped at 'confirmed'/'triaged'/'identified' has real work
  // waiting -- nothing else in this UI could get back to it before this,
  // short of knowing its run_id and calling the API by hand.
  const RESUMABLE = new Set(['confirmed', 'triaged', 'identified']);
  $('#runsTable').innerHTML = table(['Cycle', 'Started', 'Frames', 'Stage', ''],
    runs.map((r) => [
      `<td>${esc(r.cycle_label || r.run_id)}</td>`,
      `<td class="n">${esc((r.started_at || '').slice(0, 10))}</td>`,
      `<td class="n">${nf(r.image_count)}</td>`,
      `<td>${esc(r.stage)}</td>`,
      `<td>${RESUMABLE.has(r.stage) && r.image_count
        ? `<button type="button" class="resumeRunBtn" data-run="${esc(r.run_id)}">Continue</button>`
        : ''}</td>`]));
  $$('#runsTable .resumeRunBtn').forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = 'Starting…';
      const r = await runPipeline(btn.dataset.run);
      S.newRun = { step: 'running', runId: btn.dataset.run, jobId: r.job_id };
      $('#newRunBody').hidden = false;
      nrRender();
    };
  });

  const a = S.run.alerts || {};
  const hot = (a.act || 0) + (a.watch || 0);
  const t = $('#tallyAlerts');
  t.textContent = hot || '';
  t.classList.toggle('hot', (a.act || 0) > 0);
};

/* ── new-run wizard ────────────────────────────────────────────────────────
   Stage 1 (ingest) and Stage 2A (the motion prefilter) are both real code
   now; this is the interface for them. Stage 2B (a detector) still isn't
   built, so the wizard says so rather than pretending the run is finished. */
S.newRun = { step: 'form' };

$('#newRunToggle').addEventListener('click', () => {
  const body = $('#newRunBody');
  body.hidden = !body.hidden;
  if (!body.hidden) nrRender();
});

const NR_STEPS = [['form', 'Scan'], ['preflight', 'Resolve'], ['confirmed', 'Confirm'],
                   ['triaged', 'Prefilter']];

function nrStepper(active) {
  return `<div class="step">${NR_STEPS.map(([k, label]) =>
    `<span class="${k === active ? 'on' : ''}">${esc(label)}</span>`).join('→')}</div>`;
}

async function nrRender() {
  const el = $('#newRunBody');
  const nr = S.newRun;

  if (nr.step === 'form') {
    el.innerHTML = `
      <div class="hr"></div>
      ${nrStepper('form')}
      <p class="note">Points this node at a folder on disk and reports what it
         understood. Nothing is moved and no station is guessed until you
         confirm the next screen.</p>
      <div class="grid g2" style="margin-top:var(--s3)">
        <label class="field">Folder path
          <div style="display:flex;gap:var(--s2)">
            <input id="nrRoot" type="text" placeholder="E:\\CAMERA_TRAP\\2026_08\\RAW" style="flex:1">
            <button type="button" id="nrBrowseBtn">Browse…</button>
          </div>
        </label>
        <label class="field">Cycle label (optional)
          <input id="nrCycle" type="text" placeholder="Phase-IV 2026 Cycle III">
        </label>
      </div>
      <div id="nrBrowsePanel" hidden></div>
      <div class="toolbar" style="margin-top:var(--s4)">
        <button class="primary" id="nrScanBtn">Scan</button>
        <span class="note" id="nrMsg"></span>
      </div>`;
    $('#nrScanBtn').onclick = nrScan;
    $('#nrBrowseBtn').onclick = nrBrowseNative;
    return;
  }

  if (nr.step === 'preflight') {
    const p = nr.preflight;
    el.innerHTML = `
      ${nrStepper('preflight')}
      <div class="grid g4">
        ${[
          ['Files found', nf(p.files_found), ''],
          ['Will be ingested', nf(p.images_ingested),
            p.duplicate_count ? `${nf(p.duplicate_count)} duplicate content skipped` : 'no duplicates'],
          ['Corrupt or unreadable', nf(p.corrupt_count), 'counted, never crashed the scan'],
          ['Estimated processing time', `${p.estimated_seconds}s`,
            `at ${p.estimated_seconds_per_image_assumed}s/frame — an assumption, not a measurement`],
        ].map(([k, v, s], i) => `<div class="card stat${i === 1 ? ' lead' : ''}">
            <div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>
            <div class="sub">${esc(s)}</div></div>`).join('')}
      </div>
      ${p.cross_run_duplicates ? `
        <div class="banner" style="margin-top:var(--s4)">
          <b>${nf(p.cross_run_duplicates)} file(s) already ingested by an earlier run</b>
          <span>${esc(p.cross_run_note)}${p.images_ingested === 0
            ? ' There is nothing new for this scan to do — check Previous cycles below for '
              + 'the run that already has this content, rather than confirming this one.'
            : ''}</span>
        </div>` : ''}
      ${Object.keys(p.mixed_camera_folders).length ? `
        <div class="banner" style="margin-top:var(--s4)">
          <b>Mixed camera bodies</b>
          <span>${Object.keys(p.mixed_camera_folders).map(esc).join(', ')} contain frames from
            more than one camera body — the SD cards were likely mixed up. Nothing was split
            automatically; sort these by hand before relying on them.</span>
        </div>` : ''}
      ${p.unmatched_folders.length ? `
        <div class="hr"></div>
        <h2>Folders that need a station</h2>
        <p class="note">Not close enough to any known station name to assign automatically.
           Never guessed — pick a station for each, or skip the folder.</p>
        <table>${table(['Folder', 'Assign to a station'], p.unmatched_folders.map((f) => [
          `<td class="num">${esc(f)}</td>`,
          `<td><select data-folder="${esc(f)}" class="sel">
             <option value="">— choose —</option>
             <option value="__skip__">Skip this folder</option>
             ${nr.stations.map((s) =>
               `<option value="${esc(s.station_id)}">${esc(s.name)} (${esc(s.station_id)})</option>`
             ).join('')}
           </select></td>`]))}</table>` : ''}
      <div class="toolbar" style="margin-top:var(--s4)">
        <button class="primary" id="nrConfirmBtn">Confirm</button>
        <button id="nrCancelBtn">Start over</button>
        <span class="note" id="nrMsg"></span>
      </div>`;
    $('#nrConfirmBtn').onclick = nrConfirm;
    $('#nrCancelBtn').onclick = () => { S.newRun = { step: 'form' }; nrRender(); };
    return;
  }

  if (nr.step === 'confirmed') {
    const c = nr.confirmResult;
    el.innerHTML = `
      ${nrStepper('confirmed')}
      <div class="grid g3">
        ${[['Images assigned to a station', nf(c.resolved_images)],
           ['Images skipped', nf(c.skipped_images)],
           ['Bursts grouped into events', nf(c.events)]]
          .map(([k, v]) => `<div class="card stat"><div class="k">${esc(k)}</div>
            <div class="v">${esc(v)}</div></div>`).join('')}
      </div>
      <div class="toolbar" style="margin-top:var(--s4)">
        <button class="primary" id="nrTriageBtn">Run triage</button>
        <span class="note">Two passes: a fast motion prefilter clears the obvious blanks,
          then the real detector looks at everything that's left and sorts it into
          animal, person, vehicle, or blank.</span>
      </div>`;
    $('#nrTriageBtn').onclick = nrTriage;
    return;
  }

  if (nr.step === 'running') {
    el.innerHTML = `
      ${nrStepper('triaged')}
      <p class="note">Running as one background job now -- triage, identification,
         occupancy and alerts, without further clicks. Progress is shown below. This
         screen will not update itself; switch tabs and come back anytime, or wait
         here.</p>`;
    return;
  }

  if (nr.step === 'triaged') {
    const t = nr.triageResult;
    el.innerHTML = `
      ${nrStepper('triaged')}
      <p class="note">Stage A — motion prefilter</p>
      <div class="grid g4">
        ${[['Quarantined — confidently blank', nf(t.quarantined)],
           ['Passed to the detector', nf(t.awaiting_detector + t.subject + t.person + t.vehicle + t.blank_by_detector)],
           ['Unreadable', nf(t.unreadable)],
           ['Still without a station', nf(t.skipped_no_station)]]
          .map(([k, v]) => `<div class="card stat"><div class="k">${esc(k)}</div>
            <div class="v">${esc(v)}</div></div>`).join('')}
      </div>
      <p class="note" style="margin-top:var(--s4)">Stage B — the real detector</p>
      <div class="grid g4">
        ${[['Has an animal', nf(t.subject), 'ready for Identify a photo'],
           ['Has a person', nf(t.person), 'blurred, routed off the tiger pipeline'],
           ['Has a vehicle', nf(t.vehicle), ''],
           ['Blank after all', nf(t.blank_by_detector), 'Stage A was not sure; Stage B was']]
          .map(([k, v, s]) => `<div class="card stat"><div class="k">${esc(k)}</div>
            <div class="v">${esc(v)}</div>${s ? `<div class="sub">${esc(s)}</div>` : ''}</div>`).join('')}
      </div>
      <p class="note" style="margin-top:var(--s3)">${esc(t.note)}</p>
      ${t.skipped_no_station ? `<p class="note">${nf(t.skipped_no_station)} frame(s) from
        skipped folders have no station and were left untouched — the motion prefilter has
        no per-station history to compare them against.</p>` : ''}
      ${t.subject ? `
        <div class="hr"></div>
        <h2>${nf(t.subject)} frame(s) have a real animal in them</h2>
        <p class="note">A bulk scan never runs matching on its own — Stage 3 looks at one
           photo at a time, on purpose. Click Identify next to a frame below to run it
           through the catalogue right now, using the file already on this machine.</p>
        <table>${table(['When', 'Station', 'Result'], nr.subjectImages.map((im) => [
          `<td class="n">${esc((im.captured_at || '').slice(0, 16).replace('T', ' '))}</td>`,
          `<td>${esc(im.station_name || im.station_id || '—')}</td>`,
          `<td data-image="${esc(im.image_id)}"><button type="button" class="idRunImgBtn"
             data-image="${esc(im.image_id)}">Identify</button></td>`,
        ]))}</table>` : ''}
      <div class="toolbar" style="margin-top:var(--s4)">
        <button class="primary" id="nrDoneBtn">View this run</button>
        <button id="nrAnotherBtn">Scan another folder</button>
      </div>`;
    $$('#newRunBody .idRunImgBtn').forEach((btn) => {
      btn.onclick = () => nrIdentifyRunImage(nr.runId, btn.dataset.image, btn.parentElement);
    });
    $('#nrDoneBtn').onclick = async () => {
      $('#newRunBody').hidden = true;
      S.newRun = { step: 'form' };
      await RENDER.run();
    };
    $('#nrAnotherBtn').onclick = () => { S.newRun = { step: 'form' }; nrRender(); };
    return;
  }
}

async function nrBrowseNative() {
  const btn = $('#nrBrowseBtn');
  btn.disabled = true;
  const prevLabel = btn.textContent;
  btn.textContent = 'Waiting for Explorer…';
  try {
    const r = await api('/api/fs/native-browse', { method: 'POST' });
    if (r.available) {
      if (r.path) $('#nrRoot').value = r.path;
      return;   // cancelled with nothing picked: leave the field as it was
    }
  } catch { /* fall through to the in-page picker below */ }
  finally {
    btn.disabled = false;
    btn.textContent = prevLabel;
  }
  nrBrowse(null);   // this Python install has no native dialog available
}

async function nrBrowse(path) {
  const panel = $('#nrBrowsePanel');
  panel.hidden = false;
  panel.innerHTML = '<p class="note">Reading folder…</p>';
  try {
    const r = await api(`/api/fs/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`);
    panel.innerHTML = `
      <div class="card pad" style="margin-top:var(--s2)">
        <div class="toolbar" style="margin:0">
          <b>${esc(r.path || 'Drives')}</b>
          <div class="spacer"></div>
          ${r.path ? '<button type="button" id="nrUseBtn">Use this folder</button>' : ''}
          <button type="button" id="nrCloseBtn">Close</button>
        </div>
        <div class="hr"></div>
        <div class="folderlist">
          ${r.path ? `<button type="button" class="folderitem" data-path="${esc(r.parent || '')}">.. up</button>` : ''}
          ${r.entries.map((e) => `<button type="button" class="folderitem" data-path="${esc(e.path)}">${esc(e.name)}</button>`).join('')}
          ${r.entries.length ? '' : '<p class="note">No subfolders here.</p>'}
        </div>
      </div>`;
    $$('#nrBrowsePanel .folderitem').forEach((b) => {
      b.onclick = () => nrBrowse(b.dataset.path || null);
    });
    if (r.path) $('#nrUseBtn').onclick = () => { $('#nrRoot').value = r.path; panel.hidden = true; };
    $('#nrCloseBtn').onclick = () => { panel.hidden = true; };
  } catch (e) {
    panel.innerHTML = `<p class="note">${esc(e.detail || 'Could not read that folder.')}</p>`;
  }
}

async function nrScan() {
  const root_path = $('#nrRoot').value.trim();
  const cycle_label = $('#nrCycle').value.trim();
  if (!root_path) { $('#nrMsg').textContent = 'Enter a folder path first.'; return; }
  $('#nrScanBtn').disabled = true;
  $('#nrMsg').textContent = 'Scanning…';
  try {
    const [pf, stations] = await Promise.all([
      api('/api/runs', { method: 'POST',
        body: { reserve_id: S.reserve.reserve_id, root_path, cycle_label: cycle_label || null } }),
      api(`/api/stations?reserve_id=${S.reserve.reserve_id}`),
    ]);
    S.newRun = { step: 'preflight', runId: pf.run_id, preflight: pf, stations };
    nrRender();
    guideNotify('scan');
  } catch (e) {
    $('#nrScanBtn').disabled = false;
    $('#nrMsg').textContent = e.detail || 'Could not scan that folder — check the path.';
  }
}

async function nrConfirm() {
  const nr = S.newRun;
  const station_assignments = {};
  const skip_folders = [];
  $$('#newRunBody select[data-folder]').forEach((sel) => {
    if (sel.value === '__skip__') skip_folders.push(sel.dataset.folder);
    else if (sel.value) station_assignments[sel.dataset.folder] = sel.value;
  });
  $('#nrConfirmBtn').disabled = true;
  try {
    const c = await api(`/api/runs/${nr.runId}/confirm`,
      { method: 'POST', body: { station_assignments, skip_folders } });
    S.newRun = { ...nr, step: 'confirmed', confirmResult: c };
    nrRender();
    guideNotify('confirm');
  } catch (e) {
    $('#nrConfirmBtn').disabled = false;
    $('#nrMsg').textContent = e.detail || 'Every folder needs a station or a skip.';
  }
}

async function nrTriage() {
  const nr = S.newRun;
  $('#nrTriageBtn').disabled = true;
  // v0.1.1 stopped here and handed the officer a table with one Identify
  // button per animal frame -- 945 of them on the seeded demo, roughly
  // 4,000 scaled to a 50,000-frame import. Triage is now the first step of
  // one background job that carries on through identification, occupancy
  // and alerts without further clicks.
  if (!nr.manualMode) {
    const r = await runPipeline(nr.runId);
    S.newRun = { ...nr, step: 'running', jobId: r.job_id };
    nrRender();
    return;
  }
  const t = await api(`/api/runs/${nr.runId}/triage/run`, { method: 'POST', body: {} });
  const subjectImages = t.subject
    ? await api(`/api/runs/${nr.runId}/images?status=subject`) : [];
  S.newRun = { ...nr, step: 'triaged', triageResult: t, subjectImages };
  nrRender();
  guideNotify('triage');
}

async function nrIdentifyRunImage(runId, imageId, cell) {
  cell.innerHTML = 'Identifying…';
  try {
    const r = await api(`/api/runs/${runId}/images/${imageId}/identify`,
      { method: 'POST', body: { actor: 'field' } });
    const [title] = DECISION_COPY[r.decision] || [r.decision, ''];
    cell.innerHTML = r.ind_id
      ? `${esc(title)} — <a href="#tigers" data-ind="${esc(r.ind_id)}">${esc(r.ind_id)}</a>`
      : esc(title);
    guideNotify('identify');
    if (r.ind_id) { await RENDER.tigers?.(); }
    if (r.queue_id) { await RENDER.review?.(); }
  } catch (e) {
    cell.innerHTML = `Failed: ${esc(e.message)}`;
  }
}

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
    const conf = t.mean_confidence != null ? `${Math.round(t.mean_confidence * 100)}%` : '—';
    const lastSeen = (t.last_seen || '').slice(0, 10) || '—';
    return `<button class="tiger" data-ind="${esc(t.ind_id)}">
      ${flankThumb(t.ind_id)}
      <div>
        <div class="id">${esc(t.ind_id)}${t.label ? ` · ${esc(t.label.toUpperCase())}` : ''}</div>
        <div class="meta">${nf(t.crop_count)} sightings · ${nf(t.station_count)} cameras ·
           ${conf} match confidence · last seen ${esc(lastSeen)}</div>
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
        ${flankThumb(t.ind_id, 'wide tall')}
        <div style="flex:1">
          <h1>${esc(t.ind_id)}${t.label ? ` · ${esc(t.label.toUpperCase())}` : ''}</h1>
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

/* ── identify: one photo, the full Stage 3 chain ──────────────────────────
   POSTs multipart, so it bypasses the JSON-only api() helper and calls
   fetch() directly -- letting the browser set its own multipart boundary
   header rather than the JSON one api() always sends. */
RENDER.identify = async () => {
  $('#idResult').innerHTML = '';
  $('#idMsg').textContent = '';
};

const DECISION_COPY = {
  auto: ['Auto-matched', 'Confident enough to accept without review.'],
  review: ['Sent for review', 'Not confident enough to accept on its own.'],
  enroll: ['Enrolled as a new tiger', 'No existing entity of this flank scored close enough to call a match.'],
  refuse: ['Could not be matched', 'The crop did not clear the quality gate.'],
  no_animal_detected: ['No animal found', 'Stage B found nothing to identify in this photo.'],
};

function drawIdResult(r) {
  const [title, sub] = DECISION_COPY[r.decision] || [r.decision, ''];
  const candidates = (r.candidates || []).map((c) => `
    <div class="pair" style="margin-top:var(--s2)">
      ${flankThumb(c.ind_id)}
      <div style="flex:1"><div class="k">${esc(c.ind_id)}</div>
      <div class="e">score ${c.score.toFixed(3)}</div></div>
    </div>`).join('');
  $('#idResult').innerHTML = `
    <div class="card pad">
      <h2>${esc(title)}</h2>
      <p class="note">${esc(sub)}</p>
      <dl class="kv" style="margin-top:var(--s3)">
        ${r.side ? `<dt>Flank</dt><dd>${r.side === 'L' ? 'Left' : 'Right'}</dd>` : ''}
        ${r.quality != null ? `<dt>Crop quality</dt><dd>${r.quality.toFixed(2)}</dd>` : ''}
        <dt>Reason</dt><dd>${esc(r.reason || '')}</dd>
        ${r.ind_id ? `<dt>Individual</dt><dd><a href="#tigers">${esc(r.ind_id)}</a></dd>` : ''}
        ${r.queue_id ? `<dt>Review queue</dt><dd><a href="#review">open in Review</a></dd>` : ''}
      </dl>
      ${candidates ? `<h2 style="margin-top:var(--s4)">Top candidates</h2>${candidates}` : ''}
    </div>`;
}

$('#idSubmit').addEventListener('click', async () => {
  const fileInput = $('#idFile');
  const msg = $('#idMsg');
  if (!fileInput.files.length) { msg.textContent = 'Choose a photo first.'; return; }

  const form = new FormData();
  form.append('file', fileInput.files[0]);
  form.append('reserve_id', S.reserve.reserve_id);
  form.append('actor', $('#idActor').value || 'field');

  msg.textContent = 'Identifying…';
  $('#idSubmit').disabled = true;
  try {
    const res = await fetch('/api/identify/upload', { method: 'POST', body: form });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || `${res.status}`);
    }
    const r = await res.json();
    msg.textContent = '';
    drawIdResult(r);
    guideNotify('identify');
    if (r.ind_id) { await RENDER.tigers?.(); }
    if (r.queue_id) { await RENDER.review?.(); }
  } catch (e) {
    msg.textContent = `Failed: ${e.message}`;
  } finally {
    $('#idSubmit').disabled = false;
  }
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
  // Nothing to compare against (no candidates) is the common case while
  // no side classifier exists -- "Not any of these" is the only sensible
  // choice, so it starts pre-selected instead of making every single one
  // of these require an extra click before Confirm does anything.
  if (!it.candidates.length && reviewPick === 0) reviewPick = 'new';
  el.innerHTML = `
    <div class="review">
      <div class="card pad">
        <h2>Unidentified frame</h2>
        <div class="pair" style="margin-top:var(--s4)">
          ${cropThumb(it.crop_id, it.crop_id, 'wide tall')}
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
            ${flankThumb(c.ind_id)}
            <div style="flex:1">
              <div class="k">${esc(c.ind_id)} <kbd>${i + 1}</kbd></div>
              <div class="e">match ${c.score.toFixed(3)} · ${esc(c.evidence)}</div>
            </div></button>`).join('')}
        <button class="cand" data-pick="new" aria-pressed="${reviewPick === 'new'}">
          <div style="flex:1"><div class="k">Not any of these <kbd>N</kbd></div>
          <div class="e">record as a tiger not yet in the catalogue</div></div></button>
        <div class="toolbar" style="margin-top:var(--s4)">
          <button class="primary" id="confirmBtn">Confirm <kbd>↵</kbd></button>
          <button id="skipBtn">Skip <kbd>J</kbd></button>
          <span class="note" id="reviewMsg"></span>
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
  // "Not any of these" creates a genuinely new individual server-side
  // (edge/app.py's decide() route) -- it never reuses a candidate's
  // ind_id, and there may be no candidates at all to reuse (the common
  // case while no side classifier exists: every crop is compared against
  // nothing and sent straight here).
  if (!isNew && !it.candidates[reviewPick]) {
    $('#reviewMsg').textContent = 'Pick a candidate, or "Not any of these", first.';
    return;
  }
  const ind = isNew ? null : it.candidates[reviewPick].ind_id;
  const r = await api(`/api/review/${it.queue_id}/decide`,
    { method: 'POST', body: { ind_id: ind, actor: 'director', new_individual: isNew } });
  reviewItems.splice(reviewIdx, 1);
  reviewPick = 0;
  if (reviewIdx >= reviewItems.length) reviewIdx = Math.max(0, reviewItems.length - 1);
  $('#tallyReview').textContent = reviewItems.length || '';
  drawReview();
  if (isNew) await RENDER.tigers?.();
  return r;
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
  /* One request instead of two, and it carries what the old map faked:
     which cameras stopped mid-cycle, which were installed this cycle, and
     where each tiger's centroid was last cycle. All three used to be
     either hardcoded (`const DEAD = new Set(['PN-C-008','PN-C-009'])`) or
     simply absent. */
  const d = await api(`/api/runs/${S.run.run_id}/map`);
  window.PugMap.render($('#mapSvg'), { ...d, focus: S.mapFocus || null },
    (ind) => { S.mapFocus = ind; RENDER.map(); });

  $('#occGeojson').href = `/api/runs/${S.run.run_id}/occupancy/export.geojson`;
  $('#occCsv').href = `/api/runs/${S.run.run_id}/occupancy/export.csv`;

  const occ = d.occupancy;
  if (!occ.length) {
    /* Two empty states used to look identical and mean opposite things:
       "nothing moved this cycle" and "this stage has never run against
       your data". */
    $('#occTable').innerHTML = `<div class="card empty">
      <strong>No home ranges yet</strong>
      Nothing in this run has been identified to an individual, so there is
      nothing to map. Run the pipeline on this run, or identify some frames
      first.</div>`;
    return;
  }

  $('#occTable').innerHTML = table(
    ['Tiger', 'Cameras', 'Area km²', 'Visits', 'Camera-days', 'Note'],
    occ.map((o) => [
      `<td class="n"><button class="linkish" data-ind="${esc(o.ind_id)}">${esc(o.ind_id)}</button></td>`,
      `<td class="n">${o.station_set.length}</td>`,
      `<td class="n">${o.area_km2 ?? '—'}</td>`,
      `<td class="n">${nf(o.event_count)}</td>`,
      `<td class="n">${o.effort_days}</td>`,
      `<td style="color:var(--muted)">${esc(o.insufficient_reason || '')}</td>`]));

  $('#occTable').querySelectorAll('[data-ind]').forEach((b) =>
    b.addEventListener('click', () => {
      S.mapFocus = S.mapFocus === b.dataset.ind ? null : b.dataset.ind;
      RENDER.map();
    }));
};

/* ── background jobs ──────────────────────────────────────────────────────
   v0.1.1 ran a 50,000-frame import inside the HTTP request that asked for
   it: no progress, no cancel, no resume, and a browser timeout partway
   through left the run in a state nothing recorded. */

async function runPipeline(runId, actor = 'director') {
  const r = await api(`/api/runs/${runId}/pipeline`,
    { method: 'POST', body: { actor } });
  S.job = r.job_id;
  pollJob(r.job_id);
  return r;
}

let jobTimer = null;
async function pollJob(jobId) {
  clearTimeout(jobTimer);
  let j;
  try { j = await api(`/api/jobs/${jobId}`); }
  catch { return; }
  drawJob(j);
  if (['queued', 'running', 'paused'].includes(j.state)) {
    jobTimer = setTimeout(() => pollJob(jobId), 1500);
  } else {
    // The wizard has nothing of its own to say once its job is the thing
    // actually running -- close it so the tab's own stats/previous-cycles
    // table (which RENDER[S.view] is about to refresh) is what's visible,
    // instead of a stale "Confirm" screen with a permanently disabled
    // button and no sign anything happened.
    if (S.newRun?.jobId === jobId) {
      $('#newRunBody').hidden = true;
      S.newRun = { step: 'form' };
    }
    RENDER[S.view]?.();
  }
}

function drawJob(j) {
  const host = $('#jobPanel');
  if (!host) return;
  host.hidden = false;
  const pct = Math.round((j.progress || 0) * 100);
  const eta = j.eta_seconds != null
    ? `${Math.floor(j.eta_seconds / 60)} min ${Math.round(j.eta_seconds % 60)} s left`
    : 'estimating…';
  const stage = j.detail?.stage ? ` · ${esc(j.detail.stage)}` : '';
  host.className = `job ${esc(j.state)}`;
  host.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <strong>${esc(j.kind)}${stage}</strong>
      <span class="num">${esc(j.state)}</span>
    </div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="meta">
      <span>${nf(j.done_count)} of ${nf(j.total)} · ${pct}%</span>
      <span>${j.state === 'running' ? eta : ''}</span>
    </div>
    ${j.error ? `<p class="note">${esc(j.error)}</p>` : ''}
    ${j.failed_count ? `<div class="deadletters">${nf(j.failed_count)} frames could not
      be read and were skipped. They are listed under this run — nothing was
      silently dropped.</div>` : ''}
    ${j.state === 'running'
      ? `<button class="btn ghost" id="jobCancel">Stop after this batch</button>`
      : ''}
    ${j.state === 'paused'
      ? `<button class="btn" id="jobResume">Resume from ${nf(j.done_count)}</button>`
      : ''}`;
  $('#jobCancel')?.addEventListener('click', async () => {
    await api(`/api/jobs/${j.job_id}/cancel`, { method: 'POST', body: { actor: 'director' } });
  });
  $('#jobResume')?.addEventListener('click', async () => {
    await api(`/api/jobs/${j.job_id}/resume`, { method: 'POST', body: { actor: 'director' } });
    pollJob(j.job_id);
  });
}

/* Pick up a job that was already running when the page was loaded — a
   50,000-frame run outlives a browser tab, and closing the tab must not
   look like the work stopped. */
(async () => {
  try {
    const { active } = await api('/api/jobs');
    if (active?.length) pollJob(active[0].job_id);
  } catch { /* server not up yet */ }
})();

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
      ${flankThumb(a.ind_id)}
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
  const d = await api(`/api/sync/status?reserve_id=${S.reserve.reserve_id}`);
  const canBundle = d.bundle_sync_enabled;
  $('#syncBody').innerHTML = `
    <div class="card pad">
      <h2>This node</h2>
      <dl class="kv" style="margin-top:var(--s3)">
        <dt>Node ID</dt><dd>${esc(d.node_id)}</dd>
        <dt>Bundle sync</dt><dd>${canBundle ? 'Ready' : 'Off'}</dd>
        ${canBundle ? '' : `<dt>Reason</dt><dd>${esc(d.bundle_sync_reason)}</dd>`}
        <dt>Rows waiting to go out</dt><dd class="num">${d.pending_rows ?? '—'}</dd>
      </dl>
      <div class="hr"></div>
      <p>A bundle leaves this machine as a single signed file -- the runs and
         images this node has written, nothing more. It can travel over a
         network when there is one, or on a USB drive when there is not.
         Applying the same file twice changes nothing.</p>
      <div class="toolbar" style="margin-top:var(--s4)">
        ${canBundle
          ? `<a class="btn primary" href="/api/sync/bundle?reserve_id=${S.reserve.reserve_id}" download>Write bundle to drive</a>`
          : '<button disabled>Write bundle to drive</button>'}
        <input type="file" id="syncApplyFile" accept="application/json" hidden>
        <button id="syncApplyBtn" ${canBundle ? '' : 'disabled'}>Apply a bundle</button>
        <span class="note" id="syncApplyNote"></span>
      </div>
    </div>
    <div class="hr"></div>
    <div class="card pad">
      <h2>Central tier</h2>
      <dl class="kv" style="margin-top:var(--s3)">
        <dt>Status</dt><dd>Off</dd>
        <dt>Reason</dt><dd>${esc(d.central_tier_reason)}</dd>
      </dl>
    </div>`;

  $('#syncApplyBtn')?.addEventListener('click', () => $('#syncApplyFile').click());
  $('#syncApplyFile')?.addEventListener('change', async () => {
    const file = $('#syncApplyFile').files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('actor', 'director');
    $('#syncApplyNote').textContent = 'Applying…';
    try {
      const r = await fetch('/api/sync/bundle/apply', { method: 'POST', body: fd });
      const stats = await r.json();
      if (!r.ok) throw new Error(stats.detail || 'apply failed');
      $('#syncApplyNote').textContent =
        `${nf(stats.inserted)} new, ${nf(stats.unchanged)} already had, `
        + `${nf(stats.conflict_resolved)} resolved by conflict.`;
      RENDER.sync();
    } catch (e) {
      $('#syncApplyNote').textContent = e.message;
    }
  });
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
  guideInit();
  window.addEventListener('hashchange', route);
  route();
})();
