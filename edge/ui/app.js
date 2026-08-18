/* PUGMARK · front end
   No framework, no build step, no network beyond this machine. */

'use strict';

const S = { reserve: null, run: null, config: null, tigers: [], sup: false, user: null };

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
    if (r.status === 401 && !path.startsWith('/api/auth/login') && !path.startsWith('/api/auth/forgot-password')) {
      showAuth('login');
    }
    let detail = '';
    let data = null;
    try {
      data = await r.json();
      detail = (typeof data?.detail === 'string') ? data.detail : (data?.error || '');
    } catch { /* no body */ }
    const err = new Error(`${r.status} ${path}${detail ? `: ${detail}` : ''}`);
    err.detail = detail;
    err.status = r.status;
    err.data = data;
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
  for (const ch of String(id || 'TIGER')) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
  const rand = () => ((h = Math.imul(h ^ (h >>> 15), 2246822507)) >>> 0) / 4294967296;
  const bands = [];
  let y = 0;
  while (y < 100) {
    const gap = 3 + rand() * 6;
    const w = 2.0 + rand() * 4.8;
    y += gap;
    if (y + w > 100) break;
    const skew = (rand() - 0.5) * 3.2;
    const cp = (rand() - 0.5) * 4.0;
    bands.push(`<path d="M0,${y.toFixed(1)} Q10,${(y + cp).toFixed(1)} 20,${(y + skew).toFixed(1)}
      L20,${(y + skew + w).toFixed(1)} Q10,${(y + cp + w).toFixed(1)} 0,${(y + w).toFixed(1)} Z"/>`);
    y += w;
  }
  const cleanId = String(id || 'tig').replace(/[^a-zA-Z0-9_-]/g, '_');
  return `<svg class="stripe ${cls}" viewBox="0 0 20 100" preserveAspectRatio="none"
    role="img" aria-label="Flank pattern for ${esc(id)}" style="border-radius:4px;overflow:hidden">
    <defs>
      <linearGradient id="pelt-${cleanId}" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="#fdf4e4"/>
        <stop offset="25%" stop-color="#f59e0b"/>
        <stop offset="70%" stop-color="#d97706"/>
        <stop offset="100%" stop-color="#b45309"/>
      </linearGradient>
    </defs>
    <rect width="20" height="100" fill="url(#pelt-${cleanId})"/>
    <g fill="#2b1810" opacity="0.92">${bands.join('')}</g>
  </svg>`;
}

function flankThumb(id, cls = '', eager = false, honest = false) {
  const loading = eager ? 'eager' : 'lazy';
  // fetchpriority matters separately from loading: "eager" only stops the
  // browser from DEFERRING an off-screen image, it says nothing about
  // ordering among the many other eager images a busy page (30+ tiger
  // cards, the provisional table) can be requesting at the same moment.
  // Browsers cap concurrent connections per origin at 6 -- confirmed via
  // the server's own access log: a real identify-photo comparator request
  // never reached the server at all, not even as a failure, consistent
  // with it losing that queue and being dropped when its view changed
  // before a connection freed up. fetchpriority="high" tells the browser
  // this specific request should jump the queue.
  const priority = eager ? ' fetchpriority="high"' : '';
  // honest=true skips the stripeRail() decoration entirely -- for a merge
  // decision specifically, a generated pattern that merely LOOKS like a
  // photo is worse than no photo, because it invites comparing two fakes
  // and believing something real was checked.
  if (honest) {
    const compact = cls.includes('tall') ? '' : ' compact';
    return `<span class="stripe-thumb ${cls}">
      <img src="/api/individuals/${encodeURIComponent(id)}/thumbnail" class="real-crop"
           data-source="/api/individuals/${encodeURIComponent(id)}/source"
           alt="Flank photo for ${esc(id)}" loading="${loading}"${priority}
           onload="this.classList.add('loaded');
                   this.parentNode.querySelector('.no-photo').hidden=true"
           onerror="this.style.display='none'">
      ${/* "No photo on file" is the DEFAULT state, hidden only once a real
            image has decoded. It used to be revealed by onerror, which left a
            third case with no handler: a request that neither loads nor errors
            (slow response, or a 200 carrying something undecodable). That drew
            an empty grey rectangle beside a tiger's name on the one screen
            asking an officer to compare photographs, with nothing saying the
            photo was missing. Defaulting to the honest message means the only
            way to show a photo frame is to actually have a photo. */ ''}
      <span class="no-photo${compact}">No photo on file</span>
    </span>`;
  }
  return `<span class="stripe-thumb ${cls}">
    <img src="/api/individuals/${encodeURIComponent(id)}/thumbnail" class="real-crop"
         data-source="/api/individuals/${encodeURIComponent(id)}/source"
         alt="Flank photo for ${esc(id)}" loading="${loading}"${priority}
         onload="this.classList.add('loaded')" onerror="this.remove()">
    ${stripeRail(id, cls)}
  </span>`;
}

/* ── photo viewer ──────────────────────────────────────────────────────
   Officers were identifying tigers by reading catalogue IDs, because the
   photographs beside those IDs are 34px wide -- and until the lazy-load
   deadlock was fixed, were not loading at all. An ID is not evidence of
   anything; the stripes are. Any thumbnail in the application now opens
   full size on click.

   Delegated from document rather than bound per thumbnail: these are
   rendered by a dozen different RENDER functions and re-rendered constantly,
   and a listener attached at render time is a listener leaked at re-render. */
(function photoViewer() {
  let box = null;

  function close() {
    box?.classList.remove('open');
    document.body.classList.remove('viewer-open');
  }

  function open(src, caption, sub, full) {
    if (!box) {
      box = document.createElement('div');
      box.className = 'photo-viewer';
      box.innerHTML = `
        <button class="pv-close" type="button" aria-label="Close">✕</button>
        <figure>
          <img alt="">
          <figcaption>
            <span class="pv-title"></span><span class="pv-sub"></span>
            <span class="pv-switch" hidden>
              <button type="button" data-view="crop" class="on">Flank crop</button>
              <button type="button" data-view="full">Whole photo</button>
            </span>
          </figcaption>
        </figure>`;
      document.body.appendChild(box);
      box.addEventListener('click', (e) => {
        const sw = e.target.closest('.pv-switch button');
        if (sw) {
          /* A crop is the stripes with the context cut away -- it cannot show
             where the animal was, what else was in frame, or how much the
             detector had to work with. The whole frame is the evidence the
             crop was taken FROM, so a reviewer can reach it without leaving
             the comparison. */
          const img = box.querySelector('img');
          const want = sw.dataset.view;
          img.src = want === 'full' ? box.dataset.full : box.dataset.crop;
          box.querySelectorAll('.pv-switch button').forEach(b =>
            b.classList.toggle('on', b === sw));
          return;
        }
        if (e.target === box || e.target.closest('.pv-close')) close();
      });
    }
    box.dataset.crop = src;
    box.dataset.full = full || '';
    box.querySelector('img').src = src;
    box.querySelector('.pv-title').textContent = caption || '';
    box.querySelector('.pv-sub').textContent = sub || '';
    // the toggle only appears when a source frame is actually recorded
    const sw = box.querySelector('.pv-switch');
    sw.hidden = !full;
    sw.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.view === 'crop'));
    box.classList.add('open');
    document.body.classList.add('viewer-open');
    box.querySelector('.pv-close').focus();
  }

  document.addEventListener('click', (e) => {
    const thumb = e.target.closest('.stripe-thumb');
    if (!thumb) return;
    const img = thumb.querySelector('img.real-crop');
    // nothing to enlarge: a frame with no photo behind it must not pretend
    if (!img || !img.complete || !img.naturalWidth) return;
    // a thumbnail often sits inside a button (a candidate row, a tiger card).
    // Opening the viewer must not also fire that button.
    e.preventDefault();
    e.stopPropagation();
    const row = thumb.closest('[data-pick], .tiger-card-rich, .cand');
    // `.k` holds the id AND the keyboard-shortcut badge, so take the first
    // span rather than the whole cell, or the caption reads "PENCH-... Key 1"
    const title = thumb.dataset.viewerTitle
      || row?.querySelector('.k > span:first-child')?.textContent?.trim()
      || row?.querySelector('h3')?.textContent?.trim()
      || (img.alt || '').replace(/^Flank photo for\s*/i, '')
      || 'Photo';
    open(img.currentSrc || img.src, title, thumb.dataset.viewerSub || '',
         img.dataset.source || '');
  }, true);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && box?.classList.contains('open')) close();
  });
})();

/* Same idea, keyed by crop_id instead of individual -- the review screen
   shows the frame under review before it has been matched to anyone.
   No stripeRail() fallback here, deliberately. stripeRail draws bands from
   a hash of the id -- it is decoration, not a photograph. Behind a real
   crop that fails to load it becomes an invented flank pattern presented
   to a reviewer as the frame under review, and nothing on screen says so.
   A reviewer must either see the actual pixels or be told plainly that
   there are none. */
function cropThumb(cropId, fallbackId, cls = '',
                    noPhotoMsg = 'No photo saved for this frame. Do not confirm an identity — use Skip and report it.',
                    eager = false) {
  // See flankThumb()'s comment on fetchpriority -- loading="eager" alone
  // does not win a busy page's connection-pool queue.
  const priority = eager ? ' fetchpriority="high"' : '';
  return `<span class="stripe-thumb ${cls}">
    <img src="/api/crops/${encodeURIComponent(cropId)}/image" class="real-crop"
         data-source="/api/crops/${encodeURIComponent(cropId)}/source"
         alt="Photo under review" loading="${eager ? 'eager' : 'lazy'}"${priority}
         onload="this.classList.add('loaded')"
         onerror="this.style.display='none';
                  this.parentNode.querySelector('.no-photo').hidden=false">
    <span class="no-photo" hidden>${esc(noPhotoMsg)}</span>
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

/* ── readiness banner ─────────────────────────────────────────────────────
   The pipeline fails closed when a model file is missing: every animal
   becomes "species unknown" and every frame is sent to human review. That is
   the correct safety behaviour, but on screen it is indistinguishable from
   "there were no tigers on this card", and the only warning lived on the Ops
   tab. If the software cannot recognise a tiger right now, the person using
   it has to be told at the top of the screen, in words, before they spend an
   afternoon reviewing frames by hand. */
async function readinessBanner() {
  let host = document.getElementById('readyBanner');
  if (!host) {
    host = document.createElement('div');
    host.id = 'readyBanner';
    host.style.cssText = 'position:sticky;top:0;z-index:60';
    document.querySelector('main')?.prepend(host);
  }
  let data;
  try { data = await api('/api/health/ready'); } catch { return; }
  const bad = (data.checks || []).filter(c => !c.ok && c.blocking);
  const warn = (data.checks || []).filter(c => !c.ok && !c.blocking);
  if (!bad.length && !warn.length) { host.innerHTML = ''; return; }

  const isBlocked = bad.length > 0;
  host.innerHTML = `
    <div class="card pad" style="border-left:5px solid ${isBlocked ? '#dc2626' : '#d97706'};
         background:${isBlocked ? 'var(--bad-bg)' : 'var(--warn-bg)'};margin-bottom:var(--s3)">
      <h2 style="margin:0 0 6px">${isBlocked
        ? 'This computer cannot recognise tigers yet'
        : 'Some checks are working at reduced accuracy'}</h2>
      <p class="note" style="margin:0 0 8px">${isBlocked
        ? 'Photos will still be sorted into empty / animal / people, but every animal will be '
          + 'sent to the review list instead of being matched to a tiger. Nothing is lost — '
          + 'the photos are safe and can be processed again once this is fixed.'
        : 'The software will run, but some results will be less certain than normal.'}</p>
      <ul style="margin:0;padding-left:18px">
        ${[...bad, ...warn].map(c => `<li style="margin-bottom:4px">
          <b>${esc(c.check)}</b> — ${esc(c.detail || '')}
          ${c.fix ? `<br><span class="note">To fix: <code>${esc(c.fix)}</code></span>` : ''}
        </li>`).join('')}
      </ul>
    </div>`;
}

/* ── routing ───────────────────────────────────────────────────────────── */
const RENDER = {};

function route() {
  const name = (location.hash.replace('#', '') || 'run');
  S.view = name;
  $$('.view').forEach((v) => v.classList.toggle('on', v.dataset.view === name));
  $$('.nav a').forEach((a) => {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  RENDER[name]?.().catch((e) => console.error(name, e));
  guideOnView(name);

  // Every view starts at its own top. Without this the document keeps the
  // scroll offset of whatever was open before, so arriving at the map from
  // "Locate on Map" landed on the Territories table at the bottom of the
  // page -- the map was rendered correctly, just scrolled past. It only
  // looked intermittent because the first visit had nothing to inherit.
  if (S._lastRoutedView !== name) {
    S._lastRoutedView = name;
    scrollViewToTop('auto');
  }
}

/* `main` is the scrolling element, not the window -- it carries
   `overflow-y: auto` (app.css), so the document itself never scrolls and
   window.scrollTo() is a no-op here. Measured: window.scrollY stayed 0 while
   main.scrollTop was 694 and the map sat entirely above the viewport. */
function scrollViewToTop(behavior = 'smooth') {
  const main = document.querySelector('main');
  if (main && main.scrollTop) {
    try { main.scrollTo({ top: 0, behavior }); }
    catch (e) { main.scrollTop = 0; }
  }
  if (window.scrollY) window.scrollTo({ top: 0, behavior });
}

/* Open one individual's profile.
   This used to set location.hash and then, 200ms later, hope a
   `[data-ind=...]` element existed and click it. Three problems: assigning
   the hash already in the bar fires no hashchange so the view never
   re-renders; 200ms is a guess at how long an API-backed render takes; and
   the selector is not scoped to the catalogue, so it could match a map
   roster row instead of the card it meant. Awaiting the render and calling
   the detail function directly removes all three. */
window.openTigerProfile = async function openTigerProfile(indId) {
  if (location.hash !== '#tigers') {
    location.hash = '#tigers';
  }
  try {
    await RENDER.tigers?.();
    scrollViewToTop('auto');
    await showTigerDetail(indId);
  } catch (e) {
    console.error('openTigerProfile', e);
  }
};

/* Jump to the map with one individual focused.
   A plain `location.hash = '#map'` is not enough on its own: if the hash is
   ALREADY '#map' the browser fires no hashchange, so route() never runs, the
   map never re-renders, and the focus that was just set is silently dropped.
   Anything that focuses an individual has to go through here. */
window.locateOnMap = function locateOnMap(indId) {
  S.mapFocus = indId;
  // Pressing "locate" is always a request to be taken there, even if this
  // tiger is already the focused one -- see the recenter branch in map.js.
  S.mapRecenter = true;
  if (location.hash === '#map') {
    RENDER.map?.().catch((e) => console.error('map', e));
    scrollViewToTop('smooth');
  } else {
    location.hash = '#map';
    // route() only resets the scroll when the view actually CHANGES, and the
    // map may already have been visited and scrolled; make sure the map is
    // what you see after asking to locate something on it.
    setTimeout(() => scrollViewToTop('smooth'), 60);
  }
};

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

  // Keep the topbar button in step with the panel. Done here rather than in
  // the click handler because the guide also closes from its own x and from
  // finishing the last step -- one place that knows the truth, not three.
  const reopen = $('#guideReopen');
  if (reopen) {
    reopen.classList.toggle('is-on', !!S.guide.open);
    reopen.setAttribute('aria-pressed', String(!!S.guide.open));
    reopen.title = S.guide.open ? 'Hide the guided walkthrough'
                                : 'Show the guided walkthrough';
  }

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

/* ── theme ─────────────────────────────────────────────────────────────
   The initial theme is applied by an inline script in index.html's <head>
   so the page never paints light-then-dark on load. This half only owns
   the toggle and the label, and writes the choice back to localStorage so
   it survives a reload -- an operator should set the screen once, not on
   every visit. Light remains the default when nothing has been chosen. */
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function applyTheme(mode, animate = true) {
  const dark = mode === 'dark';
  const root = document.documentElement;

  const swap = () => {
    if (dark) root.setAttribute('data-theme', 'dark');
    else root.removeAttribute('data-theme');
  };

  // The switch itself answers immediately -- the control must never feel
  // like it lagged the click, even though the page behind it changes a
  // beat later.
  const btn = $('#themeToggle');
  if (btn) {
    btn.classList.toggle('is-dark', dark);
    btn.setAttribute('aria-checked', String(dark));
    btn.title = dark ? 'Switch to the bright (daytime) screen'
                     : 'Switch to the dark (low-light) screen';
  }
  try { localStorage.setItem('pugmark.theme', mode); } catch (e) { /* storage disabled */ }

  // The map has two halves that answer the theme differently. The satellite
  // imagery is a single tile set re-tinted by a CSS filter, so it follows the
  // theme on its own. The world underneath it is drawn to a canvas from
  // vector geometry, and a canvas cannot be restyled by stylesheet -- it has
  // to be redrawn, which is what this asks for.
  //
  // This used to be conditional on window.PUGMARK_BASEMAP and swapped that
  // object's `src` between two pre-toned JPEGs. Those rasters are gone (the
  // map reads a real tile pyramid now), so the condition was about to become
  // permanently false and silently strand the world map in whichever palette
  // it first drew.
  setTimeout(() => { try { window.PugMap?.refresh?.(); } catch (e) { /* map not open */ } }, 180);

  if (!animate) { swap(); return; }

  // Timed against the theme-gloom keyframes in app.css: the overlay is at
  // its darkest around 40% of 420ms, so the actual colour swap is hidden
  // under it. Flipping at t=0 (as this did before) meant the snap happened
  // in full view and the fade merely followed it, which is why the change
  // still looked abrupt no matter how the durations were tuned.
  root.classList.add('theme-transitioning');
  window.clearTimeout(applyTheme._swapT);
  window.clearTimeout(applyTheme._endT);
  applyTheme._swapT = window.setTimeout(swap, 165);
  applyTheme._endT = window.setTimeout(
    () => root.classList.remove('theme-transitioning'), 430);
}

/* ── sidebar ───────────────────────────────────────────────────────────
   Four categories, each collapsible, plus a rail that folds down to icons
   for anyone who wants the screen back. Both states persist: a rail that
   springs open again on every navigation is worse than one that never
   collapsed. Collapsing the rail does NOT collapse the sections -- their
   open/closed state is remembered separately and restored when the rail
   expands again, so folding the rail is never destructive to layout the
   operator arranged. */
function navInit() {
  const nav = $('#nav');
  if (!nav) return;

  let collapsedSections = [];
  try { collapsedSections = JSON.parse(localStorage.getItem('pugmark.nav.closed') || '[]'); }
  catch (e) { collapsedSections = []; }

  const saveSections = () => {
    const closed = [...nav.querySelectorAll('.nav-section')]
      .filter(s => s.classList.contains('is-closed'))
      .map(s => s.dataset.section);
    try { localStorage.setItem('pugmark.nav.closed', JSON.stringify(closed)); } catch (e) { /* */ }
  };

  nav.querySelectorAll('.nav-section').forEach((section) => {
    const btn = section.querySelector('.nav-group');
    const closed = collapsedSections.includes(section.dataset.section);
    section.classList.toggle('is-closed', closed);
    btn?.setAttribute('aria-expanded', String(!closed));
    btn?.addEventListener('click', () => {
      const nowClosed = !section.classList.contains('is-closed');
      section.classList.toggle('is-closed', nowClosed);
      btn.setAttribute('aria-expanded', String(!nowClosed));
      saveSections();
    });
  });

  const railBtn = $('#navCollapse');
  const applyRail = (collapsed) => {
    document.body.classList.toggle('nav-collapsed', collapsed);
    railBtn?.setAttribute('aria-expanded', String(!collapsed));
    if (railBtn) railBtn.title = collapsed ? 'Expand the menu' : 'Collapse the menu';
    try { localStorage.setItem('pugmark.nav.collapsed', collapsed ? '1' : '0'); } catch (e) { /* */ }
  };
  let railCollapsed = false;
  try { railCollapsed = localStorage.getItem('pugmark.nav.collapsed') === '1'; } catch (e) { /* */ }
  applyRail(railCollapsed);
  railBtn?.addEventListener('click',
    () => applyRail(!document.body.classList.contains('nav-collapsed')));

  /* Roll each section's counts up onto its header so a folded section
     still reports what is waiting inside it. Without this, collapsing
     Intelligence hid "Alerts 4" completely -- the one number on this screen
     that means somebody should act today. A MutationObserver rather than a
     call from each render path: the tallies are written by half a dozen
     different RENDER functions, and a badge that depends on remembering to
     refresh it is a badge that will silently go stale. */
  const OBSERVE_OPTS = {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['class'],
  };
  let badgeObserver = null;

  const syncBadges = () => {
    // MUST disconnect first. This callback writes INTO the same subtree it
    // watches (it appends a badge, sets textContent, toggles a class), so
    // an observer left connected re-fires itself on its own output and
    // loops forever -- which froze the main thread hard enough that the
    // browser offered to kill the page, and left the app blank because
    // route() never got to run. Reconnected in `finally` so an exception
    // mid-sync cannot leave the badges permanently dead either.
    if (badgeObserver) badgeObserver.disconnect();
    try {
      nav.querySelectorAll('.nav-section').forEach((section) => {
        const header = section.querySelector('.nav-group');
        if (!header) return;
        let badge = header.querySelector('.nav-group-badge');
        let total = 0;
        let hot = false;
        section.querySelectorAll('.tally').forEach((t) => {
          const n = parseInt((t.textContent || '').replace(/[^0-9]/g, ''), 10);
          if (Number.isFinite(n)) total += n;
          if (t.classList.contains('hot')) hot = true;
        });
        if (!total) { badge?.remove(); return; }
        if (!badge) {
          badge = document.createElement('span');
          badge.className = 'nav-group-badge';
          header.appendChild(badge);
        }
        const text = total > 99 ? '99+' : String(total);
        // Write only on an actual change: even with the disconnect above,
        // pointless writes cost layout on every tally update.
        if (badge.textContent !== text) badge.textContent = text;
        badge.classList.toggle('hot', hot);
      });
    } finally {
      if (badgeObserver) badgeObserver.observe(nav, OBSERVE_OPTS);
    }
  };

  syncBadges();
  badgeObserver = new MutationObserver(syncBadges);
  badgeObserver.observe(nav, OBSERVE_OPTS);

  // A section holding the current page must never stay folded away -- the
  // active item would be invisible with no indication of where it went.
  nav.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-view]');
    if (!link) return;
    const section = link.closest('.nav-section');
    if (section?.classList.contains('is-closed')) {
      section.classList.remove('is-closed');
      section.querySelector('.nav-group')?.setAttribute('aria-expanded', 'true');
      saveSections();
    }
  });
}

function themeInit() {
  // animate=false on first paint: the theme is already correct (the inline
  // <head> script set it), so a crossfade here would only be a flash on load.
  applyTheme(currentTheme(), false);
  $('#themeToggle')?.addEventListener('click',
    () => applyTheme(currentTheme() === 'dark' ? 'light' : 'dark'));
}

function guideInit() {
  const saved = guideLoad();
  $('#guideBack').onclick = () => guideGo(S.guide.index - 1);
  $('#guideNext').onclick = () => {
    if (S.guide.index === GUIDE_STEPS.length - 1) { S.guide.open = false; guideSave(); guideRender(); return; }
    guideGo(S.guide.index + 1);
  };
  $('#guideClose').onclick = () => { S.guide.open = false; guideSave(); guideRender(); };
  // Toggle, not just open: the button reported "replay the walkthrough" but
  // had no way to put it away again, so a second press did nothing visible
  // and the only exit was the small x on the panel itself.
  $('#guideReopen').onclick = () => {
    if (S.guide.open) {
      S.guide.open = false;
      guideSave();
      guideRender();
    } else {
      guideGo(saved ? S.guide.index : 0);
    }
  };

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
  // Prefer the newest run that actually finished, not just the newest run
  // row -- an Import Photos wizard closed after the preflight step (before
  // Confirm) leaves a real row behind with a started_at newer than any
  // completed cycle and finished_at still null. Without this, S.run --
  // which the map, alerts, quarantine restore and triage all read from --
  // silently points at that empty, abandoned attempt instead of the
  // reserve's actual data.
  const best = runs.find(r => r.finished_at) || runs[0];
  S.run = await api(`/api/runs/${best.run_id}`);
  const c = S.run.counts;

  $('#runTitle').textContent = S.run.cycle_label || S.run.run_id;
  $('#camtrapdpExport').href = `/api/runs/${S.run.run_id}/export/camtrapdp`;
  $('#runpill').hidden = false;
  $('#runCycle').textContent = S.run.cycle_label || '—';
  $('#runCount').textContent = `${nf(c.total)} frames`;

  const pct = c.total ? ((c.subject / c.total) * 100).toFixed(1) : '0';

  /* Every frame on the card has to appear in exactly one of these boxes.
     The old four-box layout showed only quarantined-as-"Blank" and dropped
     detector-blank, vehicle and corrupt entirely, so an officer counting
     down the column found frames missing and had no way to find out where
     they went. `other` is whatever is left after the named boxes, so the
     column always sums to the total even if a new status is added later. */
  const blankTotal = (c.quarantined || 0) + (c.blank || 0);
  const named = (c.subject || 0) + blankTotal + (c.person || 0)
              + (c.vehicle || 0) + (c.corrupt || 0);
  const other = Math.max(0, (c.total || 0) - named);

  /* Frames read is the TOTAL; everything else is a PART of it. Showing all
     five as equal cards said the opposite -- five peers in a four-column
     grid, which also left whichever one came fifth stranded alone on a
     second row. The card count is not even fixed: vehicles, damaged files
     and still-sorting appear only when non-zero, so any hard column count
     orphans something eventually.

     A total, a proportional bar and a part list says what the numbers
     actually mean, and stays symmetric at four parts or seven. */
  const parts = [
    { key: 'subject', label: 'Tiger or other animal', term: 'with an animal',
      n: c.subject || 0, note: 'kept for identification' },
    { key: 'empty', label: 'Empty — no animal', term: 'empty',
      n: blankTotal, note: 'quarantined, recoverable' },
    { key: 'person', label: 'People', term: 'with people',
      n: c.person || 0, note: 'blurred, kept out of the tiger record' },
  ];
  if (c.vehicle) parts.push({ key: 'vehicle', label: 'Vehicles', term: 'vehicles',
                              n: c.vehicle, note: 'not part of the tiger record' });
  if (c.corrupt) parts.push({ key: 'corrupt', label: 'Damaged files', term: 'damaged',
                              n: c.corrupt, note: 'could not be opened — kept, not deleted' });
  if (other) parts.push({ key: 'other', label: 'Still being sorted', term: 'still sorting',
                          n: other, note: 'not finished processing yet' });

  const total = c.total || 0;
  const share = (n) => (total ? (n / total) * 100 : 0);

  $('#runStats').innerHTML = `
    <div class="triage-breakdown">
      <div class="tb-total">
        <div class="k">Frames read</div>
        <div class="v">${nf(total)}</div>
        <div class="sub">every file found on the card</div>
      </div>

      <div class="tb-body">
        <div class="tb-bar" role="img"
             aria-label="${esc(parts.map(p => `${p.n} ${p.label}`).join(', '))}">
          ${parts.filter(p => p.n > 0).map(p =>
            `<span class="tb-seg tb-${p.key}" style="width:${share(p.n).toFixed(2)}%"
                   title="${esc(p.label)}: ${nf(p.n)}"></span>`).join('')}
        </div>
        <div class="tb-parts">
          ${parts.map(p => `
            <div class="tb-part${p.n === 0 ? ' is-zero' : ''}">
              <span class="tb-dot tb-${p.key}"></span>
              <span class="tb-n">${nf(p.n)}</span>
              <span class="tb-pct">${share(p.n).toFixed(1)}%</span>
              <span class="tb-label">${esc(p.label)}</span>
              <span class="tb-note">${esc(p.note)}</span>
            </div>`).join('')}
        </div>
      </div>
    </div>

    <div class="tb-reconcile">
      ${nf(total)} frames read = ${parts.map(p => `${nf(p.n)} ${p.term}`).join(' + ')}.
      Nothing is unaccounted for.
    </div>`;

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
            `at ${p.estimated_seconds_per_image_assumed}s/frame — hardware accelerated`],
        ].map(([k, v, s], i) => `<div class="card stat${i === 1 ? ' lead' : ''}">
            <div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>
            <div class="sub">${esc(s)}</div></div>`).join('')}
      </div>

      <!-- Removed: a "Node System Configuration & Edge Acceleration" strip.
           It restated fixed facts about the build on a screen about THIS
           scan, so it carried no information that changed with what the
           operator was looking at. It was also hardcoded twice over --
           literal model names that no longer matched edge/config.py, and
           inline hex colours (#161e1b, #064e3b) that ignored the theme and
           rendered as a dark slab across the light one. What it claimed is
           either already true elsewhere on this screen (the offline badge in
           the header) or belongs in System Health, which reads it from the
           real configuration rather than from a string in a template. -->
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
      api(`/api/reserves/${encodeURIComponent(S.reserve.reserve_id)}/stations`),
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
/* ── triage ────────────────────────────────────────────────────────────── */
let triageFilter = 'all';

RENDER.triage = async () => {
  if (!S.run) await RENDER.run();
  const d = await api(`/api/runs/${S.run.run_id}/triage`);
  const s = d.summary;
  const stageA = d.counts.stage_a || 0;
  const stageB = d.counts.stage_b || 0;

  $('#triageStats').innerHTML = [
    ['Frames Quarantined', nf(s.quarantined), 'Safely isolated in storage'],
    ['Disk Space Saved', `${nf(s.mb)} MB`, '100% recoverable'],
    ['Reviewing Time Saved', (() => {
      /* 54 frames at 3s is 0.045 hours, which nf() rounds to "0" -- a real
         saving displayed as nothing. Hours are the wrong unit for a single
         card; the number only becomes hours across a full cycle. Show the
         unit that makes the figure legible at the size it actually is. */
      const secs = (s.quarantined || 0) * (s.seconds_per_review_assumed || 3);
      if (secs >= 3600) return `${(secs / 3600).toFixed(1)} h`;
      if (secs >= 60) return `${Math.round(secs / 60)} min`;
      return `${Math.round(secs)} sec`;
    })(),
      `at ${s.seconds_per_review_assumed}s per frame, not spent looking at empty grass`],
    ['Motion Pre-Filter', nf(stageA),
      `${stageB ? Math.round((stageA / (stageA + stageB)) * 100) : 0}% caught before detector`],
  ].map(([k, v, sub], i) => `<div class="card stat${i === 0 ? ' lead' : ''}">
      <div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>
      <div class="sub">${esc(sub)}</div></div>`).join('');

  $('#restoreNote').textContent = s.restored
    ? `${nf(s.restored)} frames have been restored to active survey.` : '';

  // Wire filter buttons
  $$('#quarFilterSeg button').forEach(b => {
    b.onclick = () => {
      $$('#quarFilterSeg button').forEach(btn => btn.classList.remove('active'));
      b.classList.add('active');
      triageFilter = b.dataset.filter;
      renderQuarGallery(d.sample);
    };
  });

  renderQuarGallery(d.sample);
};

/* The frame itself, or an honest statement that it is not here.

   What this replaced: an SVG landscape drawn from a hash of the image id,
   including -- on low-confidence frames -- a hand-drawn tiger pacing through
   grass. On the one screen whose entire purpose is letting an officer check
   what the machine threw away as EMPTY, it drew an animal. It also invented
   the timestamp in the corner: `14:${rand()}:${rand()}`, rendered in the
   position and typeface of camera-trap EXIF, with no indication anywhere that
   it was fabricated.

   The real frame was already being requested underneath the drawing, from
   /api/images/{id}/file -- which cannot work, because quarantine MOVES the
   file and that route reads images.orig_path. Every preview 404'd and the
   drawing was what remained. The frames are now served from their actual
   quarantine location, and when the file genuinely is not on this machine
   the card says so instead of illustrating one. */
function cameraTrapThumb(q) {
  const confPct = Math.round((q.conf || 0) * 100);
  const isBorderline = (q.conf || 1.0) < 0.80;
  const station = q.station_id || 'unknown camera';
  const reason = q.reason || 'no reason recorded';
  // The real capture time, or an explicit absence -- never a generated one.
  const when = q.captured_at
    ? String(q.captured_at).slice(11, 19) || String(q.captured_at).slice(0, 16).replace('T', ' ')
    : 'time not recorded';

  return `
    <div class="cam-trap-canvas${isBorderline ? ' is-borderline' : ''}">
      ${q.image_id ? `
        <img class="quar-frame" src="/api/quarantine/${encodeURIComponent(q.image_id)}/image"
             alt="Quarantined frame from ${esc(station)}" loading="lazy"
             onload="this.classList.add('loaded');
                     this.parentNode.querySelector('.quar-nopreview').hidden = true"
             onerror="this.style.display='none'">` : ''}
      <div class="quar-nopreview">
        <span class="quar-nopreview-mark">no preview</span>
        <span class="quar-nopreview-why">the file is not on this machine</span>
      </div>

      <div class="cam-trap-hud top">
        <span class="hud-station">${esc(station)}</span>
        <span class="hud-time">${esc(when)}</span>
      </div>
      <div class="cam-trap-hud bottom">
        <span class="hud-reason" title="${esc(reason)}">${esc(reason)}</span>
        <span class="hud-conf">${confPct}% blank</span>
      </div>
    </div>
  `;
}

function renderQuarGallery(sample) {
  const galleryEl = document.getElementById('quarGallery');
  const tableEl = document.getElementById('quarTable');
  if (!galleryEl) return;

  let items = sample || [];
  if (triageFilter === 'lowconf') {
    items = items.filter(q => q.conf < 0.80);
  }

  if (!items.length) {
    galleryEl.innerHTML = '<div class="card empty" style="grid-column:1/-1">No frames match the selected filter.</div>';
    if (tableEl) tableEl.innerHTML = '';
    return;
  }

  galleryEl.innerHTML = items.slice(0, 16).map(q => {
    const confPct = Math.round(q.conf * 100);
    const fileName = (q.orig_path || '').split('/').pop() || q.orig_path;
    const isBorderline = q.conf < 0.80;
    return `
      <div class="quar-card">
        <div class="quar-thumb">
          ${cameraTrapThumb(q)}
        </div>
        <div class="quar-body">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <strong style="font-size:12.5px">${esc(q.station_id)}</strong>
            <span class="tag ${isBorderline ? 'prov' : ''}" style="font-size:10.5px">${confPct}% Blank</span>
          </div>
          <div class="quar-conf-bar">
            <div class="quar-conf-fill" style="width:${confPct}%;background:${isBorderline ? '#f59e0b' : '#10b981'}"></div>
          </div>
          <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(fileName)}">
            ${esc(fileName)}
          </div>
          <div style="font-size:11px;color:var(--text);margin-top:2px">${esc(q.reason)}</div>
          <button type="button" class="btn restore-single-btn" data-file="${esc(q.orig_path)}" style="margin-top:6px;font-size:11.5px;padding:4px 8px">
            Restore Frame
          </button>
        </div>
      </div>`;
  }).join('');

  galleryEl.querySelectorAll('.restore-single-btn').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-ring"></span>Restoring…';
      try {
        await api(`/api/runs/${S.run.run_id}/quarantine/restore`, {
          method: 'POST',
          body: { actor: 'director' }
        });
        btn.textContent = 'Restored ✓';
        setTimeout(() => RENDER.triage(), 600);
      } catch (e) {
        btn.textContent = 'Failed';
      }
    };
  });

  // Table below
  if (tableEl) {
    tableEl.innerHTML = table(['Station', 'Confidence', 'Reason', 'Original File Path'],
      items.map((q) => [
        `<td class="n"><strong>${esc(q.station_id)}</strong></td>`,
        `<td class="n">${q.conf.toFixed(3)}</td>`,
        `<td>${esc(q.reason)}</td>`,
        `<td class="n" style="color:var(--muted)">${esc(q.orig_path)}</td>`]));
  }
}

$('#restoreBtn')?.addEventListener('click', async () => {
  const btn = $('#restoreBtn');
  btn.disabled = true;
  const r = await api(`/api/runs/${S.run.run_id}/quarantine/restore`,
    { method: 'POST', body: { actor: 'director' } });
  $('#restoreNote').textContent =
    `${nf(r.restored)} frames restored to active survey.`;
  btn.disabled = false;
  RENDER.triage();
});

$('#restoreLowConfBtn')?.addEventListener('click', async () => {
  const btn = $('#restoreLowConfBtn');
  btn.disabled = true;
  const r = await api(`/api/runs/${S.run.run_id}/quarantine/restore`,
    { method: 'POST', body: { actor: 'director', max_confidence: 0.80 } });
  $('#restoreNote').textContent =
    `${nf(r.restored)} borderline frames restored to active survey.`;
  btn.disabled = false;
  RENDER.triage();
});

/* ── tigers ────────────────────────────────────────────────────────────── */
let tigerSearchTerm = '';
let tigerSexFilter = '';
let tigerStatusFilter = '';

RENDER.tigers = async () => {
  if (!S.reserve) return;
  const rid = S.reserve.reserve_id;
  const [tigers, catHealth, provInds] = await Promise.all([
    api(`/api/individuals?reserve_id=${encodeURIComponent(rid)}`),
    api(`/api/catalogue/health?reserve_id=${encodeURIComponent(rid)}`).catch(() => null),
    api(`/api/individuals/provisional?reserve_id=${encodeURIComponent(rid)}`).catch(() => ({ items: [] }))
  ]);
  S.tigers = tigers;
  $('#tallyTigers').textContent = S.tigers.length;

  // Catalogue Health summary cards
  const healthEl = $('#catalogueHealthStats');
  if (healthEl && catHealth) {
    const both = catHealth.both_flanks?.length || 0;
    const single = catHealth.single_flank?.length || 0;
    const none = catHealth.no_flank?.length || 0;
    healthEl.innerHTML = `
      <div class="card pad">
        <div class="card-label">Dual-Flank Complete</div>
        <div class="num big" style="color:var(--ok-fg)">${both}</div>
        <div class="note">Both L and R flanks catalogued</div>
      </div>
      <div class="card pad">
        <div class="card-label">Single-Flank Biometrics</div>
        <div class="num big" style="color:#b45309">${single}</div>
        <div class="note">First-class state awaiting opposite flank</div>
      </div>
      <div class="card pad">
        <div class="card-label">Unresolved / No Flank</div>
        <div class="num big" style="color:#c5221f">${none}</div>
        <div class="note">Body crops without verified flank side</div>
      </div>`;
  }

  // Provisional individuals & merge section
  const provCard = $('#provisionalCard');
  if (provCard) {
    const items = provInds?.items || [];
    if (items.length) {
      provCard.style.display = 'block';
      provCard.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:var(--s2)">
          <div>
            <h2 style="margin:0">Provisional Enrolments (${items.length})</h2>
            <p class="note" style="margin:2px 0 0">Auto-enrolled tigers awaiting confirmation or duplicate merge resolution.</p>
          </div>
          <div>
            <button type="button" class="btn small" id="rebuildEntitiesBtn">⟲ Rebuild Biometric Entities</button>
          </div>
        </div>
        <div class="card pad explainer" style="margin-top:var(--s3)">
          <p style="margin:0;font-size:13.5px;line-height:1.55"><strong>What "Provisional" means:</strong>
            the computer found stripes it does not recognise and made a new record. For each tiger below,
            look at the photo and pick one:</p>
          <ul style="margin:8px 0 0 20px;font-size:13.5px;line-height:1.65">
            <li><strong>It really is a tiger nobody has on record</strong> &mdash; click
              <b>Confirm New Tiger</b>. This is the common case, and you can select several at once below.</li>
            <li><strong>It is actually the same animal as one already in the catalogue</strong> (its other
              flank, or a repeat sighting that scored just under the match threshold) &mdash; click
              <b>Merge Into Existing&hellip;</b> and pick the matching tiger by its photo.</li>
            <li><strong>Not sure yet?</strong> Leave it as Provisional. Nothing stops working &mdash; it still
              shows on the map and gets tracked. You do not have to clear this list in one sitting.</li>
          </ul>
        </div>
        <div id="provBulkBar" class="toolbar" style="margin-top:var(--s3);align-items:center;gap:var(--s3);display:none">
          <span id="provBulkCount" class="note" style="margin:0"></span>
          <button type="button" class="btn small primary" id="provBulkConfirm">Confirm Selected as New Tigers</button>
          <button type="button" class="btn small" id="provBulkClear">Clear Selection</button>
        </div>
        <table style="margin-top:var(--s3)">
          <thead>
            <tr>
              <th style="width:28px"><input type="checkbox" id="provSelectAll" title="Select all"></th>
              <th>Photo</th><th>Provisional ID</th><th>First Sighted</th><th>Sightings</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            ${items.map(p => `
              <tr>
                <td><input type="checkbox" class="provRowCheck" data-ind="${esc(p.ind_id)}"></td>
                <td><div class="tiger-flank-box" style="width:44px;height:44px">${flankThumb(p.ind_id, '', true, true)}</div></td>
                <td><strong>${esc(p.ind_id)}</strong></td>
                <td>${esc((p.first_seen || '').slice(0, 10) || '—')}</td>
                <td>${nf(p.crop_count || 0)}</td>
                <td>
                  <!-- the flex lives on an inner div, never on the <td>:
                       display:flex on a table cell drops it out of the table
                       formatting context, so it stops stretching to the row
                       height and stops aligning with the cells beside it --
                       which is exactly why this row sat crooked. -->
                  <div class="cell-actions">
                    <button type="button" class="btn small primary confirmTigerBtn" data-ind="${esc(p.ind_id)}">Confirm New Tiger</button>
                    <button type="button" class="btn small mergeTigerBtn" data-ind="${esc(p.ind_id)}">Merge Into Existing&hellip;</button>
                  </div>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>`;

      provCard.querySelectorAll('.mergeTigerBtn').forEach(b => {
        b.onclick = () => _openMergeModal(b.dataset.ind);
      });

      const promoteOne = async (indId) => {
        await api(`/api/individuals/${encodeURIComponent(indId)}/promote`, { method: 'POST' });
      };
      provCard.querySelectorAll('.confirmTigerBtn').forEach(b => {
        b.onclick = async () => {
          b.disabled = true; b.innerHTML = '<span class="spinner-ring"></span>Confirming…';
          try {
            await promoteOne(b.dataset.ind);
            await RENDER.tigers();
          } catch (e) {
            alert('Could not confirm this tiger: ' + (e.detail || e.message));
            b.disabled = false; b.textContent = 'Confirm New Tiger';
          }
        };
      });

      const updateProvBulkBar = () => {
        const checked = provCard.querySelectorAll('.provRowCheck:checked');
        const bar = $('#provBulkBar');
        bar.style.display = checked.length ? 'flex' : 'none';
        $('#provBulkCount').textContent = `${checked.length} selected`;
      };
      provCard.querySelectorAll('.provRowCheck').forEach(cb => { cb.onchange = updateProvBulkBar; });
      $('#provSelectAll').onchange = (e) => {
        provCard.querySelectorAll('.provRowCheck').forEach(cb => { cb.checked = e.target.checked; });
        updateProvBulkBar();
      };
      $('#provBulkClear').onclick = () => {
        provCard.querySelectorAll('.provRowCheck').forEach(cb => { cb.checked = false; });
        $('#provSelectAll').checked = false;
        updateProvBulkBar();
      };
      $('#provBulkConfirm').onclick = async (e) => {
        const btn = e.currentTarget;
        const ids = [...provCard.querySelectorAll('.provRowCheck:checked')].map(cb => cb.dataset.ind);
        if (!ids.length) return;
        btn.disabled = true; btn.innerHTML = `<span class="spinner-ring"></span>Confirming ${ids.length}…`;
        const failed = [];
        for (const id of ids) {
          try { await promoteOne(id); } catch (e2) { failed.push(id); }
        }
        if (failed.length) alert(`Confirmed ${ids.length - failed.length} of ${ids.length}. Failed: ${failed.join(', ')}`);
        await RENDER.tigers();
      };

      $('#rebuildEntitiesBtn')?.addEventListener('click', async () => {
        try {
          const res = await api('/api/individuals/rebuild-entities', { method: 'POST', body: { reserve_id: rid } });
          alert(`Biometric entities rebuilt: ${res.entities} entities synced.`);
          await RENDER.tigers();
        } catch (e) {
          alert('Failed to rebuild entities: ' + (e.detail || e.message));
        }
      });
    } else {
      provCard.style.display = 'none';
    }
  }

  const searchEl = document.getElementById('tigerSearchInput');
  const sexEl = document.getElementById('tigerFilterSex');
  const statusEl = document.getElementById('tigerFilterStatus');

  if (searchEl) searchEl.oninput = (e) => { tigerSearchTerm = e.target.value.toLowerCase().trim(); filterAndRenderTigers(); };
  if (sexEl) sexEl.onchange = (e) => { tigerSexFilter = e.target.value; filterAndRenderTigers(); };
  if (statusEl) statusEl.onchange = (e) => { tigerStatusFilter = e.target.value; filterAndRenderTigers(); };

  filterAndRenderTigers();
};

function _openMergeModal(sourceIndId) {
  const modal = $('#mergeIndividualModal');
  if (!modal) return;
  $('#mergeSourceId').value = sourceIndId;
  const noPhotoWarn = $('#mergeSourceNoPhoto');
  noPhotoWarn.hidden = true;
  // honest=true: no stripeRail() decoration here. A generated pattern that
  // merely looks like a photo is worse than no photo for a merge decision
  // specifically -- it invites comparing two fakes and believing something
  // real was checked. eager=true: this modal is the whole point of being
  // open, not an off-screen list item -- see the loading="lazy" bug fixed
  // earlier tonight for the identify-photo comparator, same root cause.
  $('#mergeSourceThumb').innerHTML = flankThumb(sourceIndId, 'wide tall', true, true);
  const srcImg = $('#mergeSourceThumb img');
  if (srcImg) srcImg.addEventListener('error', () => { noPhotoWarn.hidden = false; }, { once: true });
  $('#mergeTargetId').value = '';
  // A merge folds one identity into another -- picking the target from ID
  // text alone was exactly the gap being reported: nothing on screen let a
  // reviewer actually compare stripes before committing to "these are the
  // same tiger". Same clickable-card pattern as the review queue's own
  // candidate picker (.cand), just populated with every other tiger here.
  const otherTigers = (S.tigers || []).filter(t => t.ind_id !== sourceIndId);
  const picker = $('#mergeTargetPicker');
  picker.innerHTML = otherTigers.length ? otherTigers.map(t => `
    <button type="button" class="cand" data-ind="${esc(t.ind_id)}" aria-pressed="false">
      ${flankThumb(t.ind_id, '', true, true)}
      <div style="flex:1">
        <div class="k">${esc(t.ind_id)}${t.label ? ` (${esc(t.label)})` : ''}</div>
        <div class="e">${t.provisional ? 'Provisional' : 'Confirmed'}${t.crop_count != null ? ` &middot; ${nf(t.crop_count)} sighting${t.crop_count === 1 ? '' : 's'}` : ''}</div>
      </div>
    </button>`).join('') : '<p class="note">No other tigers in this reserve to merge into.</p>';
  $('#mergeError').hidden = true;
  modal.hidden = false;
}

$('#mergeTargetPicker')?.addEventListener('click', (e) => {
  const btn = e.target.closest('.cand');
  if (!btn) return;
  $('#mergeTargetId').value = btn.dataset.ind;
  $('#mergeTargetPicker').querySelectorAll('.cand').forEach(b => {
    const picked = b === btn;
    b.classList.toggle('selected', picked);
    b.setAttribute('aria-pressed', String(picked));
  });
  $('#mergeError').hidden = true;
});

$('#mergeIndividualClose')?.addEventListener('click', () => { $('#mergeIndividualModal').hidden = true; });
$('#mergeIndividualCancel')?.addEventListener('click', () => { $('#mergeIndividualModal').hidden = true; });
$('#mergeIndividualModal')?.addEventListener('click', (e) => { if (e.target === $('#mergeIndividualModal')) $('#mergeIndividualModal').hidden = true; });
$('#mergeIndividualForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const source = $('#mergeSourceId').value;
  const target = $('#mergeTargetId').value;
  const errEl = $('#mergeError');
  errEl.hidden = true;
  if (!target) {
    errEl.textContent = 'Please choose a target tiger to merge into.';
    errEl.hidden = false;
    return;
  }
  try {
    await api(`/api/individuals/${encodeURIComponent(source)}/merge`, { method: 'POST', body: { into: target } });
    $('#mergeIndividualModal').hidden = true;
    await RENDER.tigers();
  } catch (err) {
    errEl.textContent = err.detail || err.message || 'Failed to merge individual.';
    errEl.hidden = false;
  }
});

function filterAndRenderTigers() {
  let list = S.tigers || [];
  if (tigerSearchTerm) {
    list = list.filter(t => (t.ind_id && t.ind_id.toLowerCase().includes(tigerSearchTerm)) ||
                            (t.label && t.label.toLowerCase().includes(tigerSearchTerm)));
  }
  if (tigerSexFilter) {
    list = list.filter(t => t.sex === tigerSexFilter || (tigerSexFilter === 'unknown' && !t.sex));
  }
  if (tigerStatusFilter === 'confirmed') {
    list = list.filter(t => !t.provisional);
  } else if (tigerStatusFilter === 'provisional') {
    list = list.filter(t => t.provisional);
  }

  const gridEl = document.getElementById('tigerGridRich');
  if (!gridEl) return;

  if (!list.length) {
    gridEl.innerHTML = '<div class="card empty" style="grid-column:1/-1">No tigers match your search criteria.</div>';
    return;
  }

  gridEl.innerHTML = list.map(t => {
    const col = window.PugMap.getTigerColor(t.ind_id);
    const sides = (t.sides || '').split(',').filter(Boolean).sort();
    const conf = t.mean_confidence != null ? `${Math.round(t.mean_confidence * 100)}%` : '—';
    const lastSeen = (t.last_seen || '').slice(0, 10) || '—';

    return `
      <div class="tiger-card-rich" data-ind="${esc(t.ind_id)}">
        <div class="tiger-card-header">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="width:12px;height:12px;border-radius:50%;background:${col.fill};border:1px solid ${col.stroke}"></span>
            <strong style="font-family:var(--f-mono);font-size:13.5px">${esc(t.ind_id)}</strong>
            ${t.label ? `<span style="font-size:12px;color:var(--muted)">· ${esc(t.label)}</span>` : ''}
          </div>
          ${t.provisional
            ? '<span class="tag prov" style="font-size:10.5px" title="Not yet reviewed by a person. See the Provisional Enrolments list above to confirm or merge it.">Provisional</span>'
            : '<span class="tag ok" style="font-size:10.5px" title="A person has confirmed this is a real, distinct tiger.">Confirmed</span>'}
        </div>
        <div class="tiger-card-body">
          <div class="tiger-flank-box">
            ${/* The catalogue grid is where an officer scans for a tiger they
                 recognise. A generated stripe pattern here is the worst place
                 of all for one: it invites recognising something that was
                 never photographed. */ ''}
            ${flankThumb(t.ind_id, '', false, true)}
          </div>
          <div style="flex:1">
            <div class="tiger-meta-grid">
              <div><span style="color:var(--muted)">Sightings:</span> <b>${nf(t.crop_count)}</b></div>
              <div><span style="color:var(--muted)">Cameras:</span> <b>${nf(t.station_count)}</b></div>
              <div><span style="color:var(--muted)">Sex:</span> <b>${esc(t.sex || 'Unknown')}</b></div>
              <div><span style="color:var(--muted)">Conf:</span> <b>${conf}</b></div>
            </div>
            <div style="margin-top:8px;font-size:11px;color:var(--muted)">
              Last seen: <b>${esc(lastSeen)}</b> &nbsp;·&nbsp; ${sides.length ? sides.join('/') + ' Flank' : 'No Flank'}
            </div>
          </div>
        </div>
      </div>`;
  }).join('');

  gridEl.querySelectorAll('.tiger-card-rich').forEach(card => {
    card.onclick = () => showTigerDetail(card.dataset.ind);
  });

  // A "Catalogue ID" link elsewhere (identify-a-photo match, etc.) sets
  // S.tigerJumpTo and switches to this view -- without this, landing here
  // just dumps a reader into a wall of 31 look-alike IDs with no way to
  // tell which one they were just told matched.
  if (S.tigerJumpTo) {
    const target = S.tigerJumpTo;
    S.tigerJumpTo = null;
    const card = gridEl.querySelector(`[data-ind="${CSS.escape(target)}"]`);
    if (card) {
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('pulse-highlight');
      setTimeout(() => card.classList.remove('pulse-highlight'), 2500);
    }
  }
}

async function showTigerDetail(indId) {
  const t = await api(`/api/individuals/${indId}`);
  const col = window.PugMap.getTigerColor(t.ind_id);
  const seen = t.sides_seen || [];
  const oneSided = seen.length === 1;

  $('#tigerDetail').innerHTML = `
    <div class="card pad" style="margin-top:var(--s4);border-top:3px solid ${col.stroke}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:var(--s3)">
        <div style="display:flex;gap:var(--s4);align-items:center">
          <div class="tiger-flank-box" style="width:96px;height:96px">
            ${/* honest=true: this is the animal's identity page. A pattern
                 generated from a hash of the id has no business standing in
                 for its photograph here. */ ''}
            ${flankThumb(t.ind_id, 'wide tall', true, true)}
          </div>
          <div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="width:14px;height:14px;border-radius:50%;background:${col.fill}"></span>
              <h1 style="margin:0">${esc(t.ind_id)}${t.label ? ` · ${esc(t.label.toUpperCase())}` : ''}</h1>
            </div>
            <p class="note" style="margin:4px 0 0">
              Sex: <b>${esc(t.sex || 'Unrecorded')}</b> &nbsp;|&nbsp;
              Age Class: <b>${esc(t.age_class || 'Adult')}</b> &nbsp;|&nbsp;
              First Seen: <b>${esc((t.first_seen || '').slice(0, 10))}</b>
            </p>
          </div>
        </div>
        <div class="toolbar" style="margin:0">
          <button type="button" class="btn" onclick="locateOnMap('${esc(t.ind_id)}')">Locate on Map</button>
          ${t.provisional ? `<button type="button" class="primary" id="promoteTigerBtn">Promote to Confirmed</button>` : ''}
        </div>
      </div>

      ${oneSided ? `<div class="banner" style="margin-top:var(--s4)">
        <b>Single Flank Catalogued:</b>
        <span>Only the ${seen[0] === 'L' ? 'left' : 'right'} flank pattern has been photographed. Opposite flank images require human association.</span>
      </div>` : ''}

      <div class="hr"></div>
      <h2>Capture History & Sightings (${t.captures.length} records)</h2>
      <table style="margin-top:var(--s2)">${table(['Date & Time', 'Camera Station', 'Reserve Zone', 'Flank', 'Match Confidence'],
        t.captures.slice(0, 50).map((c) => [
          `<td class="n">${esc((c.captured_at || '').slice(0, 16).replace('T', ' '))}
             ${c.is_night ? '<span class="tag">Night IR</span>' : ''}</td>`,
          `<td><strong>${esc(c.station_name || c.station_id)}</strong></td>`,
          `<td><span class="tag">${esc(c.zone.toUpperCase())}</span></td>`,
          `<td class="n">${c.side === 'L' ? 'Left Flank' : c.side === 'R' ? 'Right Flank' : 'Unclear'}</td>`,
          `<td class="n"><b>${(c.confidence ?? 0).toFixed(2)}</b></td>`]))}</table>
    </div>`;

  $('#promoteTigerBtn')?.addEventListener('click', async () => {
    await api(`/api/individuals/${t.ind_id}/promote`, { method: 'POST', body: { actor: 'director' } });
    alert(`${t.ind_id} promoted to confirmed catalogue!`);
    RENDER.tigers();
  });

  $('#tigerDetail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ── identify: Stage 3 Visual Pipeline ──────────────────────────────────── */
RENDER.identify = async () => {
  $('#idResult').innerHTML = '';
  $('#idMsg').textContent = '';
  resetIdSteps();

  // Populate stations dropdown
  const stnSelect = $('#idStation');
  if (stnSelect && S.reserve) {
    try {
      const stns = await api(`/api/reserves/${encodeURIComponent(S.reserve.reserve_id)}/stations`);
      stnSelect.innerHTML = '<option value="">-- Auto-detect / Unspecified --</option>' +
        stns.map(s => `<option value="${esc(s.station_id)}">${esc(s.station_id)} - ${esc(s.name || s.station_id)}</option>`).join('');
    } catch { /* stations load failed */ }
  }
};

function resetIdSteps() {
  $$('.id-step-box').forEach(b => b.classList.remove('complete'));
}

const DECISION_COPY = {
  auto: ['Auto-Matched to Catalogue', 'Stripe similarity exceeded high confidence threshold.'],
  review: ['Sent for Human Review', 'Similarity score was close but requires biometric officer confirmation.'],
  enroll: ['Enrolled as New Individual', 'No existing tiger in the catalogue matched this flank pattern.'],
  refuse: ['Crop Quality Gate Refused', 'Flank could not be cleanly extracted or animal is too distant/blurred.'],
  no_animal_detected: ['No Wildlife Detected', 'Stage B neural detector found no tiger in this photograph.'],
  unreadable: ['Image Could Not Be Read', 'The uploaded file could not be decoded as an image.'],
  non_target_species: ['Not a Tiger', 'The species classifier identified this animal as a different species.'],
  unknown_species: ['Species Not Confidently Determined', 'The species classifier could not confirm this is a tiger. Sent for human review rather than guessing.'],
  side_unknown: ['Flank Side Not Confidently Determined', 'This is a tiger, but which flank (left or right) is showing could not be confirmed. Sent for human review rather than searching the wrong catalogue.'],
};

/* Decision -> semantic pill class. Was a map of raw hex, which meant the
   identify result badge kept its light-theme greens and reds on a dark
   card. The tokens behind these classes are defined per theme in app.css. */
const DECISION_TAG_CLASS = {
  auto: 'ok', enroll: 'ok',
  review: 'warn', unknown_species: 'warn', side_unknown: 'warn',
  refuse: 'bad', no_animal_detected: 'bad', unreadable: 'bad',
  non_target_species: 'bad',
};

function drawIdResult(r) {
  const [title, sub] = DECISION_COPY[r.decision] || [r.decision, ''];
  const candidates = r.candidates || [];
  const best = candidates[0];
  const statusTag = DECISION_TAG_CLASS[r.decision] || 'ok';

  // Animate steps
  if (r.decision !== 'no_animal_detected') $('#idStep1')?.classList.add('complete');
  if (r.side) $('#idStep2')?.classList.add('complete');
  if (candidates.length || r.decision === 'enroll') $('#idStep3')?.classList.add('complete');
  $('#idStep4')?.classList.add('complete');

  const matchPercent = best ? Math.round(best.score * 100) : 0;

  // Species/side evidence is shown whenever present, not only on a full
  // match -- this is exactly the information a refused ('side_unknown',
  // 'unknown_species') result needs so it reads as an explained refusal
  // instead of a silent, unexplained non-result.
  const evidenceRows = [];
  if (r.species) {
    evidenceRows.push(`<dt>Species</dt><dd>${esc(r.species)}${r.species_confidence != null ? ` (${Math.round(r.species_confidence * 100)}% confidence)` : ' (model unavailable)'}</dd>`);
  }
  if (r.side_confidence != null || (r.side && r.side !== 'unknown')) {
    const sideLabel = r.side === 'L' ? 'Left' : r.side === 'R' ? 'Right' : 'Undetermined';
    evidenceRows.push(`<dt>Flank Side</dt><dd>${sideLabel}${r.side_confidence != null ? ` (${Math.round(r.side_confidence * 100)}% confidence)` : ''}</dd>`);
  }

  $('#idResult').innerHTML = `
    <div class="card pad" style="margin-top:var(--s4)">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:var(--s2)">
        <div>
          <h2>${esc(title)}</h2>
          <p class="note">${esc(sub)}</p>
        </div>
        <div>
          <span class="tag ${statusTag}" style="font-weight:600;padding:6px 12px">
            Decision: ${esc(r.decision.toUpperCase())}
          </span>
        </div>
      </div>

      ${evidenceRows.length ? `<dl class="kv" style="margin-top:var(--s3)">${evidenceRows.join('')}</dl>` : ''}

      <!-- Match Comparator -->
      ${best ? `
        <div class="id-match-meter">
          <span style="font-size:20px">🎯</span>
          <div>
            <strong>Top Match: ${esc(best.ind_id)} (${matchPercent}% Stripe Similarity)</strong>
            <div class="note">Matched against ${r.side === 'L' ? 'Left' : 'Right'} flank catalogue entities</div>
          </div>
        </div>

        <div class="id-comparator-box">
          <div>
            <h3>Uploaded Flank Query</h3>
            <div class="tiger-flank-box" style="width:100%;height:180px;margin-top:var(--s2)">
              ${r.crop_id ? cropThumb(r.crop_id, null, 'wide tall', 'No crop image was saved for this upload.', true) : '<div class="note" style="padding:var(--s3)">No crop was extracted from this photo.</div>'}
            </div>
            <dl class="kv" style="margin-top:var(--s3)">
              <dt>Flank Side</dt><dd>${r.side === 'L' ? 'Left Flank' : 'Right Flank'}</dd>
              <dt>Quality Score</dt><dd>${r.quality != null ? r.quality.toFixed(2) : '—'}</dd>
            </dl>
          </div>
          <div>
            <h3>Matched Catalogue Flank (${esc(best.ind_id)})</h3>
            <div class="tiger-flank-box" style="width:100%;height:180px;margin-top:var(--s2)">
              ${flankThumb(best.ind_id, 'wide tall', true)}
            </div>
            <dl class="kv" style="margin-top:var(--s3)">
              <dt>Cosine Score</dt><dd><b>${best.score.toFixed(3)}</b></dd>
              <dt>Catalogue ID</dt><dd><a href="#tigers" onclick="S.tigerJumpTo='${esc(best.ind_id)}';">${esc(best.ind_id)}</a></dd>
            </dl>
          </div>
        </div>
      ` : ''}

      <div class="toolbar" style="margin-top:var(--s4)">
        ${r.ind_id ? `<button type="button" class="primary" onclick="locateOnMap('${esc(r.ind_id)}')">View on Reserve Map</button>` : ''}
        ${r.queue_id ? `<a class="btn primary" href="#review">Open in Review Queue</a>` : ''}
      </div>
    </div>`;
}

$('#idSubmit')?.addEventListener('click', async () => {
  const fileInput = $('#idFile');
  const msg = $('#idMsg');
  if (!fileInput.files.length) { msg.textContent = 'Please choose a photo first.'; return; }

  const form = new FormData();
  form.append('file', fileInput.files[0]);
  form.append('reserve_id', S.reserve.reserve_id);
  const stnVal = $('#idStation')?.value;
  if (stnVal) form.append('station_id', stnVal);

  msg.textContent = 'Extracting features and matching against catalogue…';
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
  } catch (e) {
    msg.textContent = `Error: ${e.message}`;
  } finally {
    $('#idSubmit').disabled = false;
  }
});

/* ── review queue ──────────────────────────────────────────────────────── */
let reviewIdx = 0;
let reviewPick = 0;
let reviewItems = [];
let reviewViewMode = 'borderline';
let reviewTotalOpen = 0;
let currentClaimedQid = null;
let crossFlankItems = [];

/* The queue stores WHY an item is waiting in the pipeline's own terms:
   "score 0.835 in [t_low 0.55, t_high 0.95)". That string is the right
   thing to keep in the database -- it is evidence, and it has to stay
   reproducible years later (CLAUDE.md rule 2). It is the wrong thing to
   show a forest officer, who is being asked to make a judgement, not to
   read a threshold interval.

   Translated here rather than at the source, so the record keeps the exact
   numbers while the screen speaks plainly. The original is kept on the
   element's title, so anyone who wants the raw form can still get it. */
function plainReason(reason, item) {
  const raw = String(reason || '');
  const m = raw.match(/score\s+([\d.]+)\s+in\s+\[t_low\s+([\d.]+),\s*t_high\s+([\d.]+)\)/i);
  if (m) {
    const pct = (parseFloat(m[1]) * 100).toFixed(0);
    const hi = (parseFloat(m[3]) * 100).toFixed(0);
    return `The best match was ${pct}% — close, but under the ${hi}% the computer needs `
         + `before it will decide on its own. A person has to look.`;
  }
  if (/species is unknown/i.test(raw)) {
    return 'The computer could not confirm this is a tiger, so it never searched '
         + 'the tiger record. Check the photo and clear it if it is not usable.';
  }
  if (/flank side is unknown/i.test(raw)) {
    return 'This is a tiger, but the computer could not tell which flank is showing. '
         + 'Left and right stripes are different patterns, so it could not search '
         + 'either catalogue until someone says which side this is.';
  }
  if (/no candidate|empty catalogue|no catalogue/i.test(raw)) {
    return 'There is nothing in the record for this side yet, so there was nothing '
         + 'to compare against.';
  }
  return raw || 'The computer was not sure enough to decide on its own.';
}

/* The edge cases an officer has to be told about BEFORE choosing, because
   none of them are visible in a list of percentages:

     * which catalogue was searched. A tiger's left and right flanks carry
       unrelated stripe patterns (CLAUDE.md rule 6), so a right-side photo
       is only ever compared with right-side photos. Without saying so, a
       reviewer can reasonably wonder why an obvious tiger is not listed --
       it may simply have no photo of this side on file.
     * how decisive the top match is. 83% against 49% is a clear leader;
       83% against 81% is a coin toss wearing a percentage. The MARGIN is
       the number that should drive the decision, and it was nowhere on
       screen.
     * whether the photo is good enough to judge at all. A poor crop with a
       confident-looking score is the most dangerous item in the queue.
     * that "not sure" is a real answer. The queue must never be cleared by
       guessing (rule 8). */
function reviewGuidance(it) {
  const cands = (it.candidates || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
  const sideWord = it.side === 'L' ? 'left' : it.side === 'R' ? 'right' : null;
  const notes = [];

  if (sideWord) {
    notes.push({ kind: 'side', text:
      `Compared against <b>${sideWord}-side photos only</b> — the two flanks are different `
      + `patterns. A tiger with no ${sideWord}-side photo on file cannot appear here.` });
  }

  if (cands.length >= 2) {
    const gap = ((cands[0].score || 0) - (cands[1].score || 0)) * 100;
    if (gap >= 20) {
      notes.push({ kind: 'ok', text:
        `<b>${cands[0].ind_id}</b> leads by ${gap.toFixed(0)} points — a clear gap. `
        + `Check the stripes before saving.` });
    } else {
      notes.push({ kind: 'warn', text:
        `Top two are ${gap.toFixed(0)} points apart — the score cannot separate them. Decide on the `
        + `stripes, or choose "I am not sure". An unanswered photo beats a wrong identity.` });
    }
  } else if (cands.length === 1) {
    notes.push({ kind: 'warn', text:
      `Only one candidate was close enough to list — that is not the same as a match. `
      + `If the stripes do not line up, this tiger is not in the record yet.` });
  } else {
    notes.push({ kind: 'warn', text:
      `Nothing in the record came close. If the photo is usable, this is most likely a `
      + `tiger we have not seen before.` });
  }

  const q = it.quality ?? null;
  if (q !== null && q < 0.35) {
    notes.push({ kind: 'warn', text:
      `Crop quality is poor (${(q * 100).toFixed(0)}%). If you cannot resolve the stripes, `
      + `do not confirm an identity — skip it.` });
  }

  return `<div class="review-guidance">${notes.map(n =>
    `<div class="rg-note rg-${n.kind}">${n.text}</div>`).join('')}</div>`;
}

/* One row per individual, keeping that individual's best score. See
   edge/pipeline/identify.py::_shortlist for why the pipeline produces
   duplicates: it ranks catalogue ENTITIES, and one tiger can hold several
   photos of the same flank. */
function dedupeCandidates(cands) {
  const best = new Map();
  for (const c of (cands || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0))) {
    if (!best.has(c.ind_id)) best.set(c.ind_id, c);
  }
  return [...best.values()];
}

RENDER.review = async () => {
  const rid = S.reserve?.reserve_id;
  if (!rid) return;
  const [d, cf] = await Promise.all([
    api('/api/review/page?limit=50&offset=0'),
    api(`/api/reserves/${encodeURIComponent(rid)}/cross-flank`).catch(() => [])
  ]);
  reviewItems = d.items || [];
  crossFlankItems = cf || [];

  const totalOpen = (d.open ?? d.total ?? reviewItems.length);
  reviewTotalOpen = totalOpen;
  $('#tallyReview').textContent = totalOpen + (crossFlankItems.length ? ` (+${crossFlankItems.length} CF)` : '');
  const subBorder = $('#tallyReviewSub');
  const subCf = $('#tallyCrossFlankSub');
  if (subBorder) subBorder.textContent = totalOpen;
  if (subCf) subCf.textContent = crossFlankItems.length;

  // Tab switcher
  const btnBorder = $('#reviewTabBorderline');
  const btnCf = $('#reviewTabCrossFlank');
  if (btnBorder) {
    btnBorder.onclick = () => {
      reviewViewMode = 'borderline';
      btnBorder.classList.add('active');
      btnCf?.classList.remove('active');
      $('#reviewBody').style.display = 'block';
      $('#crossFlankBody').style.display = 'none';
      drawReview();
    };
  }
  if (btnCf) {
    btnCf.onclick = () => {
      reviewViewMode = 'crossflank';
      btnCf.classList.add('active');
      btnBorder?.classList.remove('active');
      $('#reviewBody').style.display = 'none';
      $('#crossFlankBody').style.display = 'block';
      drawCrossFlankReview();
    };
  }

  if (reviewViewMode === 'crossflank') {
    btnCf?.click();
  } else {
    drawReview();
  }
};

function drawReview() {
  const el = $('#reviewBody');
  const it = reviewItems[reviewIdx];
  if (!it) {
    if (currentClaimedQid) {
      api(`/api/review/${encodeURIComponent(currentClaimedQid)}/release`, { method: 'POST' }).catch(() => null);
      currentClaimedQid = null;
    }
    const claimEl = $('#reviewClaimStatus');
    if (claimEl) claimEl.innerHTML = '';
    el.innerHTML = `<div class="card empty"><strong>Nothing left to check</strong>
      Every photo the computer was unsure about has been answered. New ones will
      appear here after the next batch of photos is processed.</div>`;
    return;
  }

  // Claim lock for the current queue item -- also the one place that knows
  // "this is a newly-displayed item, not a re-render of the same one",
  // which is exactly when reviewPick should reset. Resetting on every
  // render instead (unconditionally) would undo a reviewer's own keyboard
  // pick (1-5, N) the instant it set one, since those handlers call
  // drawReview() again on the SAME item to redraw the selection highlight.
  const isNewItem = currentClaimedQid !== it.queue_id;
  if (isNewItem) {
    if (currentClaimedQid) {
      api(`/api/review/${encodeURIComponent(currentClaimedQid)}/release`, { method: 'POST' }).catch(() => null);
    }
    currentClaimedQid = it.queue_id;
    reviewPick = it.candidates.length ? 0 : 'new';
    api(`/api/review/${encodeURIComponent(it.queue_id)}/claim`, { method: 'POST' })
      .then(() => { const c = $('#reviewClaimStatus'); if (c) c.innerHTML = 'You are checking this photo'; })
      .catch(e => { const c = $('#reviewClaimStatus'); if (c) c.innerHTML = `<span style="color:#c5221f">⚠ ${esc(e.detail || 'Claim collision')}</span>`; });
  }

  // Species confirmed tiger, but no side is known yet (not the separate
  // case of a pose-quality failure, where the side WAS determined already
  // and confirming it again would not change anything) -- the only case
  // where a human's own read of the photo can let Stage 3 actually finish.
  const sideEligible = !it.rect_ok && it.species === 'tiger' && it.side !== 'L' && it.side !== 'R';

  const header = `
    <div class="card pad">
      <h2>${it.rect_ok ? 'Which tiger is this?' : 'The computer could not read this photo'}</h2>
      <div class="pair" style="margin-top:var(--s4)">
        <div style="display:flex;flex-direction:column;gap:var(--s2)">
          ${cropThumb(it.crop_id, it.crop_id, 'wide tall', undefined, true)}
          ${it.image_id ? `<a class="btn" target="_blank" rel="noopener"
            href="/api/images/${encodeURIComponent(it.image_id)}/file">
            See the whole photo</a>` : ''}
        </div>
        <div style="flex:1">
          <dl class="kv">
            <dt>Which side of the body</dt><dd><b>${it.side === 'L' ? 'Left side' : it.side === 'R' ? 'Right side' : 'Not clear'}</b></dd>
            <dt>Camera</dt><dd>${esc(it.station_id)}</dd>
            <dt>Date and time</dt><dd>${esc((it.captured_at || '').slice(0, 16).replace('T', ' ')) || 'Not known'}</dd>
            ${it.rect_ok ? `<dt>Photo clarity</dt><dd>${(it.quality ?? 0) >= 0.6 ? 'Good' : (it.quality ?? 0) >= 0.35 ? 'Usable' : 'Poor'}</dd>` : ''}
            <dt>Why you are being asked</dt><dd style="color:var(--pelage)"
                title="${esc(it.reason || '')}">${esc(plainReason(it.reason, it))}</dd>
          </dl>
        </div>
      </div>
    </div>`;

  if (!it.rect_ok) {
    // No embedding exists for this crop -- Stage 3 refused before it ever
    // extracted a stripe pattern (species or flank side could not be
    // confirmed), so there is no catalogue match to weigh and nothing real
    // to enrol as a new tiger from it. The photo above is the real
    // detector crop, not a placeholder -- look at it, then clear the item.
    el.innerHTML = `
      <div class="review review-no-embedding">
        ${header}
        <div class="card pad">
          ${sideEligible ? `
            <p class="note">This is a confirmed tiger, but the computer could not tell which
              flank is showing. If you can see a clear side in the photo, say which one and the
              computer will finish matching it. If no clean side is visible (facing the camera,
              from behind, mostly hidden), remove it from this list instead.</p>
            <div class="toolbar" style="margin-top:var(--s4)">
              <button class="primary" id="confirmLeftBtn">This is the Left Flank</button>
              <button class="primary" id="confirmRightBtn">This is the Right Flank</button>
            </div>
          ` : `
            <p class="note">The computer could not read a stripe pattern from this photo,
              so there is nothing here to match against the tiger record. Look at the photo
              on the left. If it is not a usable tiger photo, remove it from this list —
              the photo itself is kept and nothing is deleted.</p>
          `}
          <div class="toolbar" style="margin-top:var(--s4)">
            <button class="${sideEligible ? '' : 'primary'}" id="dismissBtn">Remove from this list <kbd>↵</kbd></button>
            <button id="skipBtn">Show me the next one <kbd>J</kbd></button>
            <span class="note" id="reviewMsg"></span>
          </div>
          <p class="note" style="margin-top:var(--s2)">Photo ${reviewIdx + 1} of ${reviewItems.length} on this page${reviewTotalOpen > reviewItems.length ? ` — ${reviewTotalOpen} waiting in total` : ''}</p>
        </div>
      </div>`;
    return;
  }

  el.innerHTML = `
    <div class="review">
      ${header}

      <div>
        <h2 style="margin-bottom:var(--s3)">Closest tigers already in the record</h2>
        ${reviewGuidance(it)}
        <p class="note" style="margin-bottom:var(--s3)">Compare the stripes in the photo above with each one below.
          Pick the one that matches, or say it is a new tiger.</p>
        ${/* Deduped here as well as in edge/pipeline/identify.py: rows already
              sitting in review_queue carry the candidate list as it was
              generated BEFORE that fix, so a queue built yesterday would still
              offer the same tiger three times. */ ''}
        ${dedupeCandidates(it.candidates).map((c, i) => `
          <button class="cand ${i === reviewPick ? 'selected' : ''}" data-pick="${i}" aria-pressed="${i === reviewPick}">
            ${/* honest=true: never fall back to stripeRail() here. That draws
                    bands from a hash of the id -- decoration, not a photograph --
                    and this is the one screen that asks an officer to COMPARE
                    STRIPES. A generated pattern presented as a reference flank
                    invites confirming a match against something invented. */ ''}
            ${flankThumb(c.ind_id, 'wide', false, true)}
            <div style="flex:1">
              <div class="k" style="display:flex;justify-content:space-between;align-items:center">
                <span>${esc(c.ind_id)}</span>
                <span class="review-kbd-badge">Key ${i + 1}</span>
              </div>
              <div class="e">
                ${/* Cosine similarity is legitimately negative when two patterns
                     point away from each other, but "-4% match" is not a thing an
                     officer can act on -- a negative reading means no resemblance,
                     not a small one, and rendering it as a percentage invites
                     reading it as "almost 0% but nearly matching". Floored at
                     zero for display; the stored score keeps its sign. */ ''}
                <b>${Math.max(0, c.score * 100).toFixed(0)}%</b>
                <span class="cand-verdict">${c.score >= 0.85 ? 'very close'
                  : c.score >= 0.7 ? 'close' : c.score >= 0.55 ? 'possible'
                  : c.score > 0 ? 'unlikely' : 'no resemblance'}</span>
                ${it.side ? `<span class="cand-side">${it.side === 'L' ? 'left' : 'right'} flank</span>` : ''}
                <span class="cand-ev">${esc(c.evidence)}</span>
              </div>
            </div>
          </button>`).join('')}

        <button class="cand ${reviewPick === 'new' ? 'selected' : ''}" data-pick="new" aria-pressed="${reviewPick === 'new'}">
          <div style="flex:1">
            <div class="k" style="display:flex;justify-content:space-between;align-items:center">
              <span>None of these — this is a tiger we have not seen before</span>
              <span class="review-kbd-badge">Key N</span>
            </div>
            <div class="e">Enrol as a new provisional tiger in catalogue</div>
          </div>
        </button>

        <div class="toolbar" style="margin-top:var(--s4)">
          <button class="primary" id="confirmBtn">Save my answer <kbd>↵</kbd></button>
          <button id="skipBtn">I am not sure — show me the next one <kbd>J</kbd></button>
          <span class="note" id="reviewMsg"></span>
        </div>
        <p class="note" style="margin-top:var(--s2)">Photo ${reviewIdx + 1} of ${reviewItems.length} on this page${reviewTotalOpen > reviewItems.length ? ` — ${reviewTotalOpen} waiting in total` : ''}</p>
      </div>
    </div>`;
}

function drawCrossFlankReview() {
  const el = $('#crossFlankBody');
  if (!crossFlankItems.length) {
    el.innerHTML = `<div class="card empty"><strong>No Cross-Flank Candidates Pending</strong>
      Opposite-flank camera captures within burst windows will appear here for human association.</div>`;
    return;
  }

  el.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:var(--s3)">
      ${crossFlankItems.map(c => {
        let ev = {};
        try { ev = typeof c.evidence === 'string' ? JSON.parse(c.evidence) : (c.evidence || {}); } catch {}
        const isPending = c.status === 'UNKNOWN_RELATIONSHIP' || c.status === 'PENDING';
        return `
          <div class="card pad">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:var(--s2)">
              <div>
                <h2>Cross-Flank Hypothesis: ${esc(c.l_ind_id)} (Left) &amp; ${esc(c.r_ind_id)} (Right)</h2>
                <p class="note">Station: <b>${esc(ev.station_id || '—')}</b> · Time Delta: <b>${ev.delta_s != null ? ev.delta_s + 's' : '—'}</b> · Confidence: <b>${Math.round((c.confidence || 0) * 100)}%</b></p>
              </div>
              <div>
                <span class="tag ${isPending ? 'prov' : ''}">${esc(c.status)}</span>
              </div>
            </div>
            <div class="grid g2" style="margin-top:var(--s3)">
              <div class="card pad" style="background:var(--surface-2)">
                <h3>Left Flank: ${esc(c.l_ind_id)}</h3>
                <div class="tiger-flank-box" style="width:100%;height:140px;margin-top:var(--s2)">
                  ${flankThumb(c.l_ind_id, 'wide tall', true)}
                </div>
              </div>
              <div class="card pad" style="background:var(--surface-2)">
                <h3>Right Flank: ${esc(c.r_ind_id)}</h3>
                <div class="tiger-flank-box" style="width:100%;height:140px;margin-top:var(--s2)">
                  ${flankThumb(c.r_ind_id, 'wide tall', true)}
                </div>
              </div>
            </div>
            ${isPending ? `
              <div class="toolbar" style="margin-top:var(--s3)">
                <button type="button" class="primary cfConfirmBtn" data-assoc="${esc(c.assoc_id)}" data-primary="${esc(c.l_ind_id)}">
                  ✓ Confirm Same Tiger (Merge into ${esc(c.l_ind_id)})
                </button>
                <button type="button" class="primary cfConfirmBtn" data-assoc="${esc(c.assoc_id)}" data-primary="${esc(c.r_ind_id)}">
                  ✓ Confirm Same Tiger (Merge into ${esc(c.r_ind_id)})
                </button>
                <button type="button" class="danger cfRejectBtn" data-assoc="${esc(c.assoc_id)}">
                  ✕ Reject (Distinct Tigers)
                </button>
              </div>` : ''}
          </div>`;
      }).join('')}
    </div>`;

  el.querySelectorAll('.cfConfirmBtn').forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/api/cross-flank/${encodeURIComponent(b.dataset.assoc)}/confirm`, {
          method: 'POST', body: { primary_ind_id: b.dataset.primary }
        });
        await RENDER.review();
      } catch (err) {
        alert('Confirm failed: ' + (err.detail || err.message));
        b.disabled = false;
      }
    };
  });

  el.querySelectorAll('.cfRejectBtn').forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/api/cross-flank/${encodeURIComponent(b.dataset.assoc)}/reject`, {
          method: 'POST', body: {}
        });
        await RENDER.review();
      } catch (err) {
        alert('Reject failed: ' + (err.detail || err.message));
        b.disabled = false;
      }
    };
  });
}

/* Without this, closing or reloading the review tab left the item on
   screen locked in state='claimed' for good, and it silently dropped out of
   the queue with no decision recorded. sendBeacon is used because a normal
   fetch is cancelled during unload. The server-side TTL sweep in
   repo_ext.expire_stale_claims() is the backstop for the cases where even
   this does not fire (crash, battery death, force quit). */
window.addEventListener('pagehide', () => {
  if (!currentClaimedQid) return;
  try {
    navigator.sendBeacon(
      `/api/review/${encodeURIComponent(currentClaimedQid)}/release`, new Blob());
  } catch { /* nothing more can be done during unload */ }
});

$('#reviewBody')?.addEventListener('click', (e) => {
  const pick = e.target.closest('[data-pick]');
  if (pick) {
    reviewPick = pick.dataset.pick === 'new' ? 'new' : Number(pick.dataset.pick);
    $$('#reviewBody .cand').forEach((b) =>
      b.classList.toggle('selected', b.dataset.pick === String(reviewPick)));
    return;
  }
  if (e.target.closest('#confirmBtn')) {
    e.preventDefault();
    confirmReview();
  }
  if (e.target.closest('#dismissBtn')) {
    e.preventDefault();
    dismissReview();
  }
  if (e.target.closest('#confirmLeftBtn')) {
    e.preventDefault();
    confirmSide('L');
  }
  if (e.target.closest('#confirmRightBtn')) {
    e.preventDefault();
    confirmSide('R');
  }
  if (e.target.closest('#skipBtn')) {
    e.preventDefault();
    if (reviewItems.length > 1) {
      reviewIdx = (reviewIdx + 1) % reviewItems.length;
      reviewPick = (reviewItems[reviewIdx]?.candidates?.length) ? 0 : 'new';
      drawReview();
    } else if (reviewItems.length === 1) {
      const msg = $('#reviewMsg');
      if (msg) {
        msg.textContent = 'This is the only item — confirm a decision to clear the queue.';
        msg.style.color = 'var(--fg-muted, #94a3b8)';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
      }
    }
  }
});

async function confirmReview() {
  const it = reviewItems[reviewIdx];
  if (!it) return;
  const isNew = reviewPick === 'new';
  if (!isNew && (!it.candidates || !it.candidates[reviewPick])) {
    const msg = $('#reviewMsg');
    if (msg) msg.textContent = 'Please select a candidate match or choose "New Tiger".';
    return;
  }
  const ind = isNew ? null : it.candidates[reviewPick].ind_id;
  const btn = $('#confirmBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-ring"></span>Recording…'; }
  try {
    const r = await api(`/api/review/${it.queue_id}/decide`,
      { method: 'POST', body: { ind_id: ind, new_individual: isNew } });
    reviewItems.splice(reviewIdx, 1);
    if (reviewIdx >= reviewItems.length) reviewIdx = Math.max(0, reviewItems.length - 1);
    reviewPick = (reviewItems[reviewIdx]?.candidates?.length) ? 0 : 'new';
    reviewTotalOpen = Math.max(0, reviewTotalOpen - 1);
    currentClaimedQid = null;
    $('#tallyReview').textContent = reviewTotalOpen || '';
    /* Refill from the server rather than draining the page to nothing. The
       page holds 50 of a possibly much larger queue; without this the screen
       emptied out and read "Queue Clear" while the backlog was untouched. */
    if (!reviewItems.length && reviewTotalOpen > 0) { await RENDER.review(); }
    else drawReview();
    return r;
  } catch (err) {
    if (btn) { btn.disabled = false; btn.innerHTML = 'Confirm Decision <kbd>↵</kbd>'; }
    const msg = $('#reviewMsg');
    if (msg) {
      msg.textContent = `Error: ${err.detail || err.message}`;
      msg.style.color = 'var(--alert-red, #ef4444)';
    }
  }
}

/* For items with no embedding (species/side never confirmed) -- closes
   the queue item without creating any assignment or catalogue entry.
   See repo.review_dismiss()'s docstring for why this must stay separate
   from confirmReview(), not a variant of it. */
async function dismissReview() {
  const it = reviewItems[reviewIdx];
  if (!it) return;
  const btn = $('#dismissBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-ring"></span>Clearing…'; }
  try {
    await api(`/api/review/${it.queue_id}/dismiss`, { method: 'POST' });
    reviewItems.splice(reviewIdx, 1);
    if (reviewIdx >= reviewItems.length) reviewIdx = Math.max(0, reviewItems.length - 1);
    reviewPick = (reviewItems[reviewIdx]?.candidates?.length) ? 0 : 'new';
    reviewTotalOpen = Math.max(0, reviewTotalOpen - 1);
    currentClaimedQid = null;
    $('#tallyReview').textContent = reviewTotalOpen || '';
    if (!reviewItems.length && reviewTotalOpen > 0) { await RENDER.review(); }
    else drawReview();
  } catch (err) {
    if (btn) { btn.disabled = false; btn.innerHTML = 'Dismiss <kbd>↵</kbd>'; }
    const msg = $('#reviewMsg');
    if (msg) {
      msg.textContent = `Error: ${err.detail || err.message}`;
      msg.style.color = 'var(--alert-red, #ef4444)';
    }
  }
}

/* A human confirms which flank is showing for a tiger the side classifier
   could not resolve on its own -- Stage 3 then actually finishes the
   analysis (rectify/embed/match/decide) instead of the item only ever
   being dismissible. See identify_upload.complete_side_unknown()'s
   docstring for why this is completion, not a fabricated result. */
async function confirmSide(side) {
  const it = reviewItems[reviewIdx];
  if (!it) return;
  const btn = $(side === 'L' ? '#confirmLeftBtn' : '#confirmRightBtn');
  const other = $(side === 'L' ? '#confirmRightBtn' : '#confirmLeftBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-ring"></span>Matching…'; }
  if (other) other.disabled = true;
  try {
    const r = await api(`/api/review/${it.queue_id}/confirm-side`, { method: 'POST', body: { side } });
    reviewItems.splice(reviewIdx, 1);
    if (reviewIdx >= reviewItems.length) reviewIdx = Math.max(0, reviewItems.length - 1);
    reviewPick = (reviewItems[reviewIdx]?.candidates?.length) ? 0 : 'new';
    reviewTotalOpen = Math.max(0, reviewTotalOpen - 1);
    currentClaimedQid = null;
    $('#tallyReview').textContent = reviewTotalOpen || '';
    const RESULT_MSG = {
      auto: `Matched to ${r.ind_id} (${r.candidates?.[0] ? Math.round(r.candidates[0].score * 100) : '?'}% stripe similarity).`,
      enroll: `No match found — enrolled as a new tiger: ${r.ind_id}.`,
      review: `Side confirmed, but the match is still borderline — sent to a fresh review item with real candidates.`,
      refuse: `Side confirmed, but the photo still could not be matched (${r.reason || 'quality too low'}).`,
    };
    if (!reviewItems.length && reviewTotalOpen > 0) { await RENDER.review(); }
    else drawReview();
    const msg = $('#reviewMsg');
    if (msg) {
      msg.textContent = RESULT_MSG[r.decision] || '';
      msg.style.color = 'var(--fg-muted, #94a3b8)';
      setTimeout(() => { if (msg) msg.textContent = ''; }, 5000);
    }
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = side === 'L' ? 'This is the Left Flank' : 'This is the Right Flank'; }
    if (other) other.disabled = false;
    const msg = $('#reviewMsg');
    if (msg) {
      msg.textContent = `Error: ${err.detail || err.message}`;
      msg.style.color = 'var(--alert-red, #ef4444)';
    }
  }
}

/* Keyboard shortcuts for review queue: 1-5, N, J, K, Enter */
document.addEventListener('keydown', (e) => {
  if (!$('#v-review')?.classList.contains('on')) return;
  if (e.target.matches('input, textarea')) return;
  const k = e.key.toLowerCase();
  if (k === 'j') {
    if (reviewItems.length > 1) {
      reviewIdx = (reviewIdx + 1) % reviewItems.length;
      reviewPick = (reviewItems[reviewIdx]?.candidates?.length) ? 0 : 'new';
      drawReview();
    } else if (reviewItems.length === 1) {
      const msg = $('#reviewMsg');
      if (msg) {
        msg.textContent = 'This is the only item — confirm a decision to clear the queue.';
        msg.style.color = 'var(--fg-muted, #94a3b8)';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
      }
    }
  } else if (k === 'k') {
    if (reviewItems.length > 0) {
      reviewIdx = (reviewIdx - 1 + reviewItems.length) % reviewItems.length;
      reviewPick = (reviewItems[reviewIdx]?.candidates?.length) ? 0 : 'new';
      drawReview();
    }
  } else if (k === 'n') {
    reviewPick = 'new';
    drawReview();
  } else if ('12345'.includes(k)) {
    const idx = Number(k) - 1;
    const it = reviewItems[reviewIdx];
    if (it && it.candidates && it.candidates[idx]) {
      reviewPick = idx;
      drawReview();
    }
  } else if (e.key === 'Enter') {
    const cur = reviewItems[reviewIdx];
    if (cur && !cur.rect_ok) dismissReview(); else confirmReview();
  } else {
    return;
  }
  e.preventDefault();
});

/* ── alerts ────────────────────────────────────────────────────────────── */
let alertFilterSev = 'all';

const KIND = {
  buffer_ward: '🔴 Tiger near village / buffer boundary',
  centroid_shift: '🟠 Territory shifted farther than usual',
  absence: '🟡 Tiger not seen during this survey cycle',
  new_station: '🔵 Tiger recorded at a new camera location',
};

// Numeric ranking so we can sort alerts by priority (lower = more urgent)
const SEVERITY_RANK = { act: 0, watch: 1, info: 2 };

// Emoji icon per severity level
const SEVERITY_ICON = { act: '🔴', watch: '🟠', info: '🟡' };

RENDER.alerts = async () => {
  if (!S.run) await RENDER.run();
  const d = await api(`/api/runs/${S.run.run_id}/alerts?suppressed=${S.sup}`);
  const c = d.counts;

  $('#alertNote').textContent =
    `${c.act} urgent · ${c.watch} watch · ${c.info} info · ${c.suppressed} suppressed`;

  // Wire severity filter tabs
  $$('#alertSeveritySeg button').forEach(b => {
    b.onclick = () => {
      $$('#alertSeveritySeg button').forEach(btn => btn.classList.remove('active'));
      b.classList.add('active');
      alertFilterSev = b.dataset.sev;
      renderAlertsList(d.items);
    };
  });

  renderAlertsList(d.items);
};

function renderAlertsList(items) {
  const listEl = document.getElementById('alertList');
  if (!listEl) return;

  let filtered = items || [];
  if (alertFilterSev !== 'all') {
    filtered = filtered.filter(a => a.severity === alertFilterSev);
  }

  if (!filtered.length) {
    listEl.innerHTML = `<div class="card empty"><strong>No alerts found</strong>
      No active alerts match the selected severity category.</div>`;
    return;
  }

  // Acknowledged alerts sink to the bottom: an operator working a list of 11
  // wants it to shrink as they go. They stay visible -- an acknowledgement is
  // a record of who accepted the alert, not a delete.
  filtered = [...filtered].sort((x, y) =>
    (x.acknowledged_at ? 1 : 0) - (y.acknowledged_at ? 1 : 0));

  listEl.innerHTML = filtered.map(a => {
    const col = window.PugMap.getTigerColor(a.ind_id);
    const ackAt = a.acknowledged_at
      ? String(a.acknowledged_at).slice(0, 16).replace('T', ' ') : null;
    const isAct = a.severity === 'act';
    const isWatch = a.severity === 'watch';
    const sevLabel = isAct ? '🚨 URGENT ACTION' : isWatch ? '⚠ WARNING WATCH' : 'ℹ ADVISORY INFO';
    const sevBadgeClass = isAct ? 'tag prov' : 'tag';
    
    const evidenceEntries = Object.entries(a.evidence || {});
    const evidenceHtml = evidenceEntries.length ? `
      <div class="alert-evidence-grid">
        ${evidenceEntries.map(([k, v]) => `
          <div class="alert-evidence-item">
            <span class="evidence-key">${esc(k.replace(/_/g, ' '))}:</span>
            <strong class="evidence-val">${esc(Array.isArray(v) ? v.join(', ') : v)}</strong>
          </div>
        `).join('')}
      </div>
    ` : '';

    return `
      <div class="alert-card-rich ${esc(a.severity)}${ackAt ? ' is-acknowledged' : ''}">
        <div class="alert-tiger-hero">
          <div class="tiger-flank-box alert-flank-pelt">
            ${/* honest=true. Without it a missing catalogue photo falls back to
                 stripeRail(), which draws bands from a hash of the id -- the
                 vertical stripe bar on every alert card was that, not a
                 photograph of the animal the alert is about. */ ''}
            ${flankThumb(a.ind_id, 'wide tall', false, true)}
          </div>
          <div class="alert-tiger-dot" style="background:${col.fill}" title="Map territory color"></div>
        </div>

        <div class="alert-card-main">
          <div class="alert-card-top">
            <div class="alert-identity-group">
              <span class="${sevBadgeClass}">${sevLabel}</span>
              <button type="button" class="linkish alert-ind-link alertViewTiger" data-ind="${esc(a.ind_id)}">
                <span class="num" style="font-weight:700;font-size:15px;color:var(--ink)">${esc(a.ind_id)}</span>
              </button>
            </div>
            <div class="alert-metrics">
              <span class="alert-metric-item">
                <span class="metric-lbl">Coverage:</span>
                <span class="num font-bold">${Math.round(a.effort_coverage * 100)}%</span>
              </span>
              <span class="metric-divider">·</span>
              <span class="alert-metric-item">
                <span class="metric-lbl">Confidence:</span>
                <span class="num font-bold">${a.confidence.toFixed(2)}</span>
              </span>
            </div>
          </div>

          <div class="alert-headline-box">
            <h3 class="alert-title">${esc(KIND[a.type] || a.type)}</h3>
            <p class="alert-desc">${esc(a.what_changed)}</p>
          </div>

          ${evidenceHtml}

          <div class="alert-card-actions">
            <button type="button" class="btn primary small alertShowMap" data-ind="${esc(a.ind_id)}">
              🗺 Inspect on Map
            </button>
            <button type="button" class="btn small alertViewTiger" data-ind="${esc(a.ind_id)}">
              🐅 Tiger Profile
            </button>
            ${ackAt ? `
              <span class="alert-ack-state" title="Acknowledgement is recorded in the audit log">
                ✓ Acknowledged by ${esc(a.acknowledged_by || 'someone')} · ${esc(ackAt)}
              </span>`
            : `
              <button type="button" class="btn small alertAckBtn" data-alert-id="${esc(a.alert_id)}"
                      title="Record that you have seen this and are handling it. The alert stays on file.">
                ✓ Acknowledge
              </button>`}
          </div>
        </div>
      </div>`;
  }).join('');

  listEl.querySelectorAll('.alertAckBtn').forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/api/alerts/${encodeURIComponent(b.dataset.alertId)}/acknowledge`, { method: 'POST' });
        await RENDER.alerts();
      } catch (e) {
        alert('Failed to acknowledge alert: ' + (e.detail || e.message));
        b.disabled = false;
      }
    };
  });

  listEl.querySelectorAll('.alertShowMap').forEach(b => {
    b.onclick = () => {
      // Same helper the catalogue uses. Assigning location.hash directly
      // fires no hashchange when the map is already open, and leaves the
      // scroller wherever it was -- see window.locateOnMap().
      locateOnMap(b.dataset.ind);
    };
  });

  listEl.querySelectorAll('.alertViewTiger').forEach(b => {
    b.onclick = () => openTigerProfile(b.dataset.ind);
  });
}

/* ── audit ─────────────────────────────────────────────────────────────── */

/* ── the audit log, in words ────────────────────────────────────────────
   The record stores what the SYSTEM did, in the system's own vocabulary:
   "location.read.precise", "review.side_confirmed", "stage3.bulk". That is
   the right thing to store -- it is exact, greppable, and stable across
   versions. It is the wrong thing to show a range officer, who is being
   asked to audit their own reserve's history, not to read event codes.

   Translated at the point of display. The raw code stays on the row (small,
   and in the CSV export) so nothing is lost. */
const AUDIT_WORDS = {
  'auth.login_success':       ['Signed in', 'auth'],
  'auth.login_failure':       ['Failed sign-in attempt', 'auth'],
  'auth.logout':              ['Signed out', 'auth'],
  'auth.locked':              ['Account locked after repeated failures', 'auth'],
  'password.change':          ['Changed a password', 'auth'],
  'password.reset':           ['Reset a password', 'auth'],
  'user.create':              ['Created a user account', 'auth'],
  'user.disable':             ['Disabled a user account', 'auth'],

  'location.read.precise':    ['Viewed exact tiger locations', 'privacy'],
  'location.read.generalised':['Viewed approximate tiger locations', 'privacy'],
  'image.read':               ['Opened a photo', 'privacy'],
  'person_image.access_refused': ['Blocked a photo containing a person', 'privacy'],

  'ingest.preflight':         ['Scanned a folder of photos', 'cycle'],
  'ingest.confirm':           ['Confirmed a photo import', 'cycle'],
  'run.stage':                ['Moved the cycle to its next stage', 'cycle'],
  'triage.stage_a':           ['Filtered out frames with no movement', 'cycle'],
  'triage.stage_b':           ['Looked for animals, people and vehicles', 'cycle'],
  'quarantine.restore':       ['Brought a quarantined frame back', 'cycle'],
  'job.create':               ['Started a background job', 'cycle'],
  'job.done':                 ['Finished a background job', 'cycle'],

  'stage3.bulk':              ['Matched flanks against the catalogue', 'tiger'],
  'identify.upload':          ['Identified an uploaded photo', 'tiger'],
  'individual.auto_enroll':   ['Enrolled a new tiger automatically', 'tiger'],
  'individual.promote':       ['Promoted a tiger to confirmed', 'tiger'],
  'individual.merge':         ['Merged two tiger records', 'tiger'],
  'review.claim':             ['Opened a photo for review', 'tiger'],
  'review.decide':            ['Confirmed which tiger a photo shows', 'tiger'],
  'review.dismiss':           ['Cleared a photo from the review list', 'tiger'],
  'review.side_confirmed':    ['Confirmed which flank was showing', 'tiger'],

  'occupancy.compute':        ['Recalculated home ranges', 'intel'],
  'alerts.generate':          ['Recalculated alerts', 'intel'],
  'alert.acknowledge':        ['Acknowledged an alert', 'intel'],

  'occupancy.export':         ['Exported home-range data', 'share'],
  'camtrapdp.export':         ['Exported a Camtrap DP package', 'share'],
  'sync.bundle.export':       ['Wrote a sync bundle to a drive', 'share'],
  'sync.bundle.apply':        ['Applied a sync bundle from another laptop', 'share'],

  'dev.seed':                 ['Replaced the database with sample data', 'danger'],
  'dev.restore_live':         ['Restored this reserve\'s own data', 'danger'],
  'seed.demo':                ['Loaded the demo dataset', 'danger'],
  'seed.bulk':                ['Loaded the large sample dataset', 'danger'],
};

const AUDIT_ICON = { auth:'\u{1F511}', privacy:'\u{1F4CD}', cycle:'\u{1F4E5}',
                     tiger:'\u{1F405}', intel:'\u{1F514}', share:'\u{1F4E4}',
                     danger:'\u26A0' };

function auditWords(action) {
  const known = AUDIT_WORDS[action];
  if (known) return { text: known[0], cat: known[1] };
  // An unmapped code must still read as a sentence rather than as an
  // identifier: "sync.bundle.verify" -> "Sync bundle verify".
  const pretty = String(action || '')
    .replace(/[._]/g, ' ')
    .replace(/^\w/, (c) => c.toUpperCase());
  return { text: pretty, cat: 'cycle' };
}

/* "3 minutes ago" answers "is this recent?" without arithmetic. The exact
   timestamp stays on the row's title attribute and in the export. */
function auditWhen(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const secs = Math.max(0, (Date.now() - t) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} hr ago`;
  const days = Math.floor(secs / 86400);
  return days === 1 ? 'yesterday' : `${days} days ago`;
}

function auditDayLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'Unknown date';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
}

/* Collapse a run of identical actions by the same person into one row.
   Ninety-nine consecutive "Viewed exact tiger locations" entries are one
   fact, not ninety-nine, and printing them separately is what buried the
   entries that actually matter. */
function auditGroup(rows) {
  const out = [];
  for (const r of rows) {
    const prev = out[out.length - 1];
    if (prev && prev.action === r.action && prev.actor === r.actor
        && auditDayLabel(prev.ts) === auditDayLabel(r.ts)) {
      prev.count += 1;
      prev.lastTs = r.ts;
      if (r.entity_id && !prev.entities.includes(r.entity_id)) prev.entities.push(r.entity_id);
    } else {
      out.push({ ...r, count: 1, lastTs: r.ts,
                 entities: r.entity_id ? [r.entity_id] : [] });
    }
  }
  return out;
}

function auditDetail(r) {
  if (r.note) return String(r.note);
  const after = r.after;
  if (!after) return '';
  let obj = after;
  if (typeof after === 'string') {
    try { obj = JSON.parse(after); } catch (e) { return after; }
  }
  if (obj && typeof obj === 'object') {
    // key: value pairs read better than raw JSON braces
    return Object.entries(obj)
      .filter(([, v]) => v !== null && v !== '' && v !== undefined)
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
      .join('  ·  ');
  }
  return String(obj);
}

let auditCategory = 'all';

RENDER.audit = async () => {
  const q = $('#auditQ')?.value.trim() || '';
  const rows = await api(`/api/audit?limit=200${q ? `&q=${encodeURIComponent(q)}` : ''}`);

  // Wire category chips
  $$('#auditCatSeg button').forEach(b => {
    b.onclick = () => {
      $$('#auditCatSeg button').forEach(btn => btn.classList.remove('active'));
      b.classList.add('active');
      auditCategory = b.dataset.cat;
      renderAuditRecords(rows);
    };
  });

  $('#auditExportBtn')?.addEventListener('click', () => {
    const csvContent = "data:text/csv;charset=utf-8," 
      + "Timestamp,Actor,Action,Entity,Notes\n"
      + rows.map(r => `"${r.ts}","${r.actor}","${r.action}","${r.entity_id || ''}","${(r.note || r.after || '').toString().replace(/"/g, '""')}"`).join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `pugmark_audit_log_${new Date().toISOString().slice(0,10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  });

  renderAuditRecords(rows);
};

function renderAuditRecords(rows) {
  let filtered = rows || [];
  if (auditCategory === 'auth') {
    filtered = filtered.filter(r => r.action && r.action.startsWith('auth.'));
  } else if (auditCategory === 'tiger') {
    filtered = filtered.filter(r => r.action && (r.action.includes('individual') || r.action.includes('match')));
  } else if (auditCategory === 'pipeline') {
    filtered = filtered.filter(r => r.action && (r.action.includes('run') || r.action.includes('triage') || r.action.includes('identify')));
  } else if (auditCategory === 'sync') {
    filtered = filtered.filter(r => r.action && r.action.includes('sync'));
  }

  const timelineEl = document.getElementById('auditTimeline');
  const tableEl = document.getElementById('auditTable');

  if (timelineEl) {
    const grouped = auditGroup(filtered).slice(0, 60);
    let lastDay = null;
    timelineEl.innerHTML = grouped.map(r => {
      const { text, cat } = auditWords(r.action);
      const day = auditDayLabel(r.ts);
      const dayHeader = day === lastDay ? '' :
        `<div class="audit-day">${esc(day)}</div>`;
      lastDay = day;
      const detail = auditDetail(r);
      const exact = (r.ts || '').replace('T', ' ').slice(0, 19);
      const ents = r.entities.slice(0, 3).map(e =>
        `<span class="audit-entity">${esc(e)}</span>`).join('');
      const more = r.entities.length > 3
        ? `<span class="audit-entity muted">+${r.entities.length - 3} more</span>` : '';
      return `
        ${dayHeader}
        <div class="audit-item audit-${esc(cat)}" title="${esc(r.action)} · ${esc(exact)}">
          <span class="audit-icon">${AUDIT_ICON[cat] || '•'}</span>
          <div class="audit-body">
            <div class="audit-line">
              <span class="audit-what">${esc(text)}</span>
              ${r.count > 1 ? `<span class="audit-count">${r.count}×</span>` : ''}
              ${ents}${more}
            </div>
            ${detail ? `<div class="audit-detail">${esc(detail)}</div>` : ''}
          </div>
          <div class="audit-meta">
            <span class="audit-who">${esc(r.actor)}</span>
            <span class="audit-ago" title="${esc(exact)}">${esc(auditWhen(r.ts))}</span>
          </div>
        </div>`;
    }).join('');
  }

  if (tableEl) {
    tableEl.innerHTML = table(['Timestamp', 'Actor', 'Action', 'Entity ID', 'Detail'],
      filtered.map((r) => [
        `<td class="n">${esc((r.ts || '').replace('T', ' ').slice(0, 19))}</td>`,
        `<td><b>${esc(r.actor)}</b></td>`,
        `<td class="n">${esc(r.action)}</td>`,
        `<td class="n">${esc(r.entity_id || '')}</td>`,
        `<td style="color:var(--muted);font-size:11.5px">${esc(r.note || (typeof r.after === 'object' ? JSON.stringify(r.after) : r.after) || '')}</td>`]));
  }
}

$('#auditQ')?.addEventListener('input', () => {
  clearTimeout($('#auditQ')._t);
  $('#auditQ')._t = setTimeout(RENDER.audit, 200);
});

/* Five questions this screen has to answer at a glance: where are the
   tigers, which one moved, what changed this cycle, is there anything to
   worry about, which camera/area caused it. Everything below is built
   toward those five, not toward showing the maximum amount of data. */
/* ── map controller ────────────────────────────────────────────────────── */
let mapSidebarTab = 'tigers';
let mapSearchQuery = '';

RENDER.map = async () => {
  if (!S.run) await RENDER.run();
  if (!S.tigers || !S.tigers.length) {
    try {
      const resId = S.reserve?.reserve_id || S.run?.reserve_id || 'PENCH-MH';
      S.tigers = await api(`/api/individuals?reserve_id=${resId}`);
    } catch (e) {
      S.tigers = [];
    }
  }
  const [d, alertData] = await Promise.all([
    api(`/api/runs/${S.run.run_id}/map`),
    api(`/api/runs/${S.run.run_id}/alerts?suppressed=false`),
  ]);
  S.mapData = d;

  const occ = d.occupancy || [];
  const stations = d.stations || [];
  const stationState = (s) => window.PugMap.stationState(s);
  const working = stations.filter((s) => stationState(s) !== 'offline').length;
  const mappedHulls = occ.filter((o) => o.hull_wkt).length;
  const totalTigersTracked = occ.filter((o) => o.event_count > 0).length || occ.length || (S.tigers && S.tigers.length) || 0;
  const totalAlertsCount = (alertData?.counts?.act || 0) + (alertData?.counts?.watch || 0) + (alertData?.counts?.info || 0) || (alertData?.items?.length || 0);

  $('#mapSummary').innerHTML = [
    ['🐅 Tigers Tracked', nf(totalTigersTracked)],
    ['⚠ Active Alerts', nf(totalAlertsCount)],
    ['📷 Operational Cameras', `${nf(working)} / ${nf(stations.length)}`],
    ['🗺 Territories Mapped', nf(mappedHulls || occ.filter(o => o.centroid_lat).length || totalTigersTracked)],
  ].map(([k, v]) => `<div class="card stat"><div class="k">${esc(k)}</div>
    <div class="v">${esc(v)}</div></div>`).join('');

  // Top highlight alert
  const top = [...alertData.items].sort(
    (a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity])[0];
  $('#mapHeadline').innerHTML = top ? `
    <div class="card pad ${esc(top.severity)}" style="margin-bottom:var(--s3)">
      <div>
        <h2>${SEVERITY_ICON[top.severity]} Territory Alert — ${esc(KIND[top.type] || top.type)}</h2>
        <p><b>${esc(top.ind_id)}</b>: ${esc(top.what_changed)}</p>
      </div>
      <div class="spacer"></div>
      <button type="button" id="mapHeadlineShow">Focus on Map</button>
    </div>` : '';
  $('#mapHeadlineShow')?.addEventListener('click', () => {
    S.mapFocus = top.ind_id;
    RENDER.map();
    $('.mapwrap')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  // Render SVG Map
  try {
    window.PugMap.render($('#mapSvg'), {
      ...d,
      focus: S.mapFocus || null,
      recenter: !!S.mapRecenter,
    }, (ind) => {
      S.mapFocus = ind;
      RENDER.map();
    });
  } catch (err) {
    console.error('PugMap render error:', err);
  }
  // one-shot: the next ordinary refresh must not yank the view back
  S.mapRecenter = false;

  // Update Sidebar Counts
  const tigerCountEl = document.getElementById('sidebarTigerCount');
  const stationCountEl = document.getElementById('sidebarStationCount');
  if (tigerCountEl) tigerCountEl.textContent = (occ.length || (S.tigers && S.tigers.length) || 0);
  if (stationCountEl) stationCountEl.textContent = stations.length;

  // Render Interactive Sidebar
  renderMapSidebar(d, alertData);

  // GeoJSON / CSV exports
  $('#occGeojson').href = `/api/runs/${S.run.run_id}/occupancy/export.geojson`;
  $('#occCsv').href = `/api/runs/${S.run.run_id}/occupancy/export.csv`;

  if (!occ.length) {
    const reason = d.empty_reason ? ` (${d.empty_reason})` : '';
    $('#occTable').innerHTML = `<div class="card empty">
      <strong>No home ranges yet${esc(reason)}</strong>
      Nothing in this run has been identified to an individual yet. Run triage or identify photos first.</div>`;
    return;
  }

  // Render area breakdown table with tiger color badges
  $('#occTable').innerHTML = table(
    ['Tiger', 'Home Range', 'Cameras Visited', 'Sightings', 'Camera-Days Effort', 'Status'],
    occ.map((o) => {
      const col = window.PugMap.getTigerColor(o.ind_id);
      const isFocused = S.mapFocus === o.ind_id;
      return [
        `<td class="n" style="font-weight:600">
           <button class="linkish" data-ind="${esc(o.ind_id)}" style="display:inline-flex;align-items:center;gap:6px;font-weight:${isFocused ? 'bold' : 'normal'}">
             <span style="width:10px;height:10px;border-radius:50%;background:${col.fill};display:inline-block"></span>
             ${esc(o.ind_id)}
           </button>
         </td>`,
        `<td class="n" style="font-weight:600">${o.area_km2 ? o.area_km2 + ' km²' : '<span style="color:var(--muted)">—</span>'}</td>`,
        `<td class="n">${o.station_set ? o.station_set.length : 0} cams</td>`,
        `<td class="n">${nf(o.event_count)} visits</td>`,
        `<td class="n">${o.effort_days || '—'}</td>`,
        `<td style="color:${o.insufficient_reason ? 'var(--muted)' : 'var(--pelage)'}">
           ${esc(o.insufficient_reason || (o.area_km2 ? 'Mapped Polygon' : 'Active'))}
         </td>`
      ];
    }));

  $('#occTable').querySelectorAll('[data-ind]').forEach((b) =>
    b.addEventListener('click', () => {
      S.mapFocus = S.mapFocus === b.dataset.ind ? null : b.dataset.ind;
      RENDER.map();
      $('.mapwrap')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }));
};

function renderMapSidebar(data, alertData) {
  const listEl = document.getElementById('mapSidebarList');
  const searchInput = document.getElementById('mapSidebarSearch');
  const tabTigers = document.getElementById('sidebarTabTigers');
  const tabStations = document.getElementById('sidebarTabStations');
  if (!listEl) return;

  tabTigers.onclick = () => {
    mapSidebarTab = 'tigers';
    tabTigers.classList.add('active');
    tabStations.classList.remove('active');
    renderMapSidebar(data, alertData);
  };

  tabStations.onclick = () => {
    mapSidebarTab = 'stations';
    tabStations.classList.add('active');
    tabTigers.classList.remove('active');
    renderMapSidebar(data, alertData);
  };

  if (searchInput) {
    searchInput.oninput = (e) => {
      mapSearchQuery = e.target.value.toLowerCase().trim();
      renderMapSidebarList(data, alertData);
    };
  }

  renderMapSidebarList(data, alertData);
}

function renderMapSidebarList(data, alertData) {
  const listEl = document.getElementById('mapSidebarList');
  if (!listEl) return;

  const alertsByInd = (alertData?.items || []).reduce((m, a) => {
    (m[a.ind_id] = m[a.ind_id] || []).push(a); return m;
  }, {});

  if (mapSidebarTab === 'tigers') {
    let tigers = (data.occupancy && data.occupancy.length) ? data.occupancy : (S.tigers || []);
    if (mapSearchQuery) {
      tigers = tigers.filter(t => (t.ind_id && t.ind_id.toLowerCase().includes(mapSearchQuery)) ||
                                  (t.label && t.label.toLowerCase().includes(mapSearchQuery)));
    }

    if (!tigers.length) {
      listEl.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:var(--s2);text-align:center">No matching tigers</div>';
      return;
    }

    listEl.innerHTML = tigers.map(t => {
      const col = window.PugMap.getTigerColor(t.ind_id);
      const isFocused = S.mapFocus === t.ind_id;
      const alerts = alertsByInd[t.ind_id] || [];
      const alertBadge = alerts.length ? `<span class="map-roster-badge alert">⚠ Alert</span>` : '';
      const camsCount = (t.station_set || []).length || t.station_count || 0;
      const areaText = t.area_km2 ? t.area_km2 + ' km²' : '—';
      const visitsText = t.event_count ? `${t.event_count} visits` : t.crop_count ? `${t.crop_count} sightings` : '0 visits';

      return `
        <div class="map-roster-item ${isFocused ? 'active' : ''}" data-ind="${esc(t.ind_id)}">
          <div class="map-roster-left">
            <span class="map-roster-swatch" style="background:${col.fill};border:1px solid ${col.stroke}"></span>
            <div>
              <div class="map-roster-title">${esc(t.ind_id)}${t.label ? ` · ${esc(t.label)}` : ''}${alertBadge}</div>
              <div style="font-size:11px;color:var(--muted)">${camsCount} cameras visited</div>
            </div>
          </div>
          <div class="map-roster-stats">
            <div style="font-weight:600;color:var(--text)">${areaText}</div>
            <div>${visitsText}</div>
          </div>
        </div>`;
    }).join('');

    // Bring the focused row into view. Arriving from "Locate on Map" with 24
    // tigers in the list, the highlighted one is usually below the fold --
    // the map moved, the roster did not, and the two disagreed about what
    // was being looked at.
    const activeRow = listEl.querySelector('.map-roster-item.active');
    if (activeRow) {
      // Scroll the LIST, not the page. scrollIntoView walks every scrollable
      // ancestor, so arriving from "Locate on Map" it scrolled the document
      // too and pushed the map itself out of view -- the one thing the user
      // had just asked to look at.
      const rowTop = activeRow.offsetTop;
      const rowBottom = rowTop + activeRow.offsetHeight;
      if (rowTop < listEl.scrollTop) {
        listEl.scrollTop = rowTop - 8;
      } else if (rowBottom > listEl.scrollTop + listEl.clientHeight) {
        listEl.scrollTop = rowBottom - listEl.clientHeight + 8;
      }
    }

    listEl.querySelectorAll('.map-roster-item').forEach(item => {
      item.onclick = () => {
        const ind = item.dataset.ind;
        // clicking the focused row clears the focus; clicking any row while
        // focused on it elsewhere re-centres on it
        S.mapFocus = S.mapFocus === ind ? null : ind;
        S.mapRecenter = !!S.mapFocus;
        RENDER.map();
      };
    });
  } else {
    // Stations tab
    let stations = data.stations || [];
    if (mapSearchQuery) {
      stations = stations.filter(s =>
        (s.name && s.name.toLowerCase().includes(mapSearchQuery)) ||
        (s.station_id && s.station_id.toLowerCase().includes(mapSearchQuery)) ||
        (s.zone && s.zone.toLowerCase().includes(mapSearchQuery))
      );
    }

    if (!stations.length) {
      listEl.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:var(--s2);text-align:center">No matching camera stations</div>';
      return;
    }

    listEl.innerHTML = stations.map(s => {
      const st = window.PugMap.stationState(s);
      const stClass = st === 'active' ? '#10b981' : st === 'offline' ? '#ef4444' : st === 'new' ? '#3b82f6' : '#a0aec0';
      const zoneLabel = (s.zone || 'reserve').toUpperCase();
      return `
        <div class="map-roster-item" data-station="${esc(s.station_id)}">
          <div class="map-roster-left">
            <span class="map-roster-swatch" style="background:${stClass}"></span>
            <div>
              <div class="map-roster-title">${esc(s.name || s.station_id)}</div>
              <div style="font-size:11px;color:var(--muted)">${esc(zoneLabel)} &nbsp;·&nbsp; ${esc(s.station_id)}</div>
            </div>
          </div>
          <div class="map-roster-stats">
            <div style="font-weight:600">${s.image_count || 0} frames</div>
            <div style="font-size:10.5px">${st === 'active' ? 'Recording' : st}</div>
          </div>
        </div>`;
    }).join('');

    listEl.querySelectorAll('.map-roster-item').forEach(item => {
      item.onclick = () => {
        // Highlight station on SVG
        const stnId = item.dataset.station;
        const pin = document.querySelector(`.stn[data-station="${stnId}"]`);
        if (pin) {
          pin.focus();
          const d = pin.dataset;
          const html = `
            <h4>📷 ${esc(d.name)}</h4>
            <div class="meta-line"><b>Zone:</b> ${esc((d.zone || 'reserve').toUpperCase())} &nbsp;|&nbsp; <b>ID:</b> ${esc(d.station)}</div>
            <div class="meta-line"><b>Status:</b> ${d.state}</div>
            <div class="meta-line"><b>Total Frames:</b> ${d.frames} photos</div>`;
          const rect = pin.getBoundingClientRect();
          window.PugMap.showTooltip?.(html, rect.left + rect.width/2, rect.top);
        }
      };
    });
  }
}

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
    const currentView = S.view || (location.hash.replace('#', '') || 'run');
    if (RENDER[currentView]) {
      try { await RENDER[currentView](); } catch (e) { console.error('Refresh error on job completion:', e); }
    }
    if (currentView !== 'run') {
      try { await RENDER.run?.(); } catch {}
    }
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
  readinessBanner().catch(() => {});
})();

/* ── ops ───────────────────────────────────────────────────────────────── */
RENDER.ops = async () => {
  const rid = S.reserve?.reserve_id;
  const [d, readyData, jobsData] = await Promise.all([
    api(`/api/ops?reserve_id=${encodeURIComponent(rid)}`),
    api('/api/health/ready').catch(e => ({ ready: false, checks: [{ check: 'Readiness Probe Error', ok: false, detail: e.message, blocking: true }] })),
    api('/api/ops/jobs').catch(() => ({ active: [], recent: [] }))
  ]);

  // Render readiness badges
  const badgesEl = $('#opsReadinessBadges');
  if (badgesEl && readyData?.checks) {
    badgesEl.innerHTML = readyData.checks.map(c => {
      const isOk = c.ok;
      const isWarn = !isOk && !c.blocking;
      const statusBadge = isOk 
        ? '<span class="tag ok">✓ Ready</span>'
        : isWarn
        ? '<span class="tag warn">⚠ Advisory</span>'
        : '<span class="tag bad">✕ Action Needed</span>';
      return `
        <div class="card pad" style="border-left:3px solid ${isOk ? '#059669' : isWarn ? '#d97706' : '#dc2626'}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <strong>${esc(c.check)}</strong>
            ${statusBadge}
          </div>
          <p class="note" style="margin:6px 0 0;font-size:12px">${esc(c.detail || '')}</p>
          ${/* A remedy belongs to a problem. Every card carried its fix command
                whether or not anything was wrong, so nine green "READY" cards
                each displayed an instruction to install something -- which
                reads as nine outstanding tasks on a healthy machine. */ ''}
          ${(!isOk && c.fix) ? `<div class="ops-fix"><span class="ops-fix-lbl">To fix</span><code>${esc(c.fix)}</code></div>` : ''}
        </div>`;
    }).join('');
  }

  const refreshBtn = $('#refreshReadinessBtn');
  if (refreshBtn) refreshBtn.onclick = () => RENDER.ops();

  await renderSeedModeStatus();

  // Operations DB maintenance actions
  const actionMsg = $('#opsActionMsg');
  const actionRes = $('#opsActionResult');

  const backupBtn = $('#opsBackupBtn');
  if (backupBtn) {
    backupBtn.onclick = async () => {
      actionMsg.textContent = 'Creating SQLite consistent backup…';
      actionRes.hidden = true;
      try {
        const res = await api('/api/ops/backup', { method: 'POST', body: {} });
        actionMsg.textContent = '';
        actionRes.innerHTML = `✓ Backup created: <code>${esc(res.path || '')}</code> (${nf(res.size_bytes || 0)} bytes)`;
        actionRes.hidden = false;
      } catch (e) {
        actionMsg.textContent = `Backup failed: ${e.detail || e.message}`;
      }
    };
  }

  const integBtn = $('#opsIntegrityBtn');
  if (integBtn) {
    integBtn.onclick = async () => {
      actionMsg.textContent = 'Running PRAGMA integrity_check…';
      actionRes.hidden = true;
      try {
        const res = await api('/api/ops/integrity');
        actionMsg.textContent = '';
        actionRes.innerHTML = res.ok
          ? `✓ Database integrity verified: OK (v${res.schema_version})`
          : `✕ Integrity check failed: ${esc(res.message || 'Corrupt pages detected')}`;
        actionRes.hidden = false;
      } catch (e) {
        actionMsg.textContent = `Integrity check failed: ${e.detail || e.message}`;
      }
    };
  }

  const cpBtn = $('#opsCheckpointBtn');
  if (cpBtn) {
    cpBtn.onclick = async () => {
      actionMsg.textContent = 'Checkpointing WAL log…';
      actionRes.hidden = true;
      try {
        const res = await api('/api/ops/checkpoint', { method: 'POST', body: {} });
        actionMsg.textContent = '';
        actionRes.innerHTML = `✓ WAL Checkpoint complete: busy=${res.busy}, log=${res.log}, checkpointed=${res.checkpointed}`;
        actionRes.hidden = false;
      } catch (e) {
        actionMsg.textContent = `Checkpoint failed: ${e.detail || e.message}`;
      }
    };
  }

  // Render jobs table
  const jobsTable = $('#opsJobsTable');
  if (jobsTable) {
    const recent = jobsData.recent || [];
    if (!recent.length) {
      jobsTable.innerHTML = '<tbody><tr><td class="note" style="text-align:center;padding:12px">No recent background jobs recorded.</td></tr></tbody>';
    } else {
      jobsTable.innerHTML = table(
        ['Job ID', 'Kind', 'State', 'Progress', 'Started / Finished', 'Actions'],
        recent.map(j => {
          const isStale = j.state === 'running' && j.checkpoint_at && (Date.now() - new Date(j.checkpoint_at).getTime() > 600000);
          const prog = `${j.done_count || 0} / ${j.total || 0}`;
          return [
            `<td><code>${esc(j.job_id)}</code></td>`,
            `<td><b>${esc(j.kind)}</b></td>`,
            `<td><span class="tag ${j.state === 'complete' ? '' : j.state === 'failed' ? 'prov' : ''}">${esc(j.state)}${isStale ? ' (Stale)' : ''}</span></td>`,
            `<td class="n">${prog}</td>`,
            `<td class="n" style="font-size:11px">${esc((j.created_at || '').slice(11, 19))} → ${esc((j.finished_at || '').slice(11, 19) || '—')}</td>`,
            `<td>${j.state === 'running' ? `<button class="btn small cancelJobBtn" data-job="${esc(j.job_id)}">Cancel</button>` : j.state === 'paused' || isStale ? `<button class="btn small primary resumeJobBtn" data-job="${esc(j.job_id)}">Resume</button>` : '—'}</td>`
          ];
        })
      );
      jobsTable.querySelectorAll('.cancelJobBtn').forEach(b => {
        b.onclick = async () => {
          await api(`/api/jobs/${encodeURIComponent(b.dataset.job)}/cancel`, { method: 'POST' });
          await RENDER.ops();
        };
      });
      jobsTable.querySelectorAll('.resumeJobBtn').forEach(b => {
        b.onclick = async () => {
          await api(`/api/jobs/${encodeURIComponent(b.dataset.job)}/resume`, { method: 'POST' });
          await RENDER.ops();
        };
      });
    }
  }

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

/* Demo data: always a full replace, run as a fresh subprocess server-side
   (edge/app.py's /api/dev/seed) rather than in-process, so a reload here
   is the reliable way back to a consistent UI -- every screen's own
   in-memory state (S.run, S.tigers, the guide, the wizard) was built
   against a database that no longer exists otherwise. Loading 'bulk' or
   'demo' has the server snapshot whatever's live first (once, not on
   every load -- see /api/dev/seed's own comment), so this is safe to use
   mid-presentation and reverse with restoreLiveData() below. 'blank' is
   the one genuinely permanent option -- no snapshot is taken for it. */
async function devSeed(which, label) {
  const permanent = which === 'blank';
  const warning = permanent
    ? `This permanently clears every reserve, run, tiger and alert on this machine. `
      + `There is no saved copy to restore afterward. Continue?`
    : `This will show ${label} instead of your current data. Whatever's currently active gets `
      + `saved automatically first, and "Show my live data" brings it back. Continue?`;
  if (!confirm(warning)) return;
  $$('#v-ops .toolbar button').forEach((b) => { b.disabled = true; });
  $('#seedMsg').textContent = 'Working…';
  try {
    await api('/api/dev/seed', { method: 'POST', body: { which } });
    location.reload();
  } catch (e) {
    $('#seedMsg').textContent = `Failed: ${e.detail || e.message}`;
    $$('#v-ops .toolbar button').forEach((b) => { b.disabled = false; });
  }
}
$('#seedBulkBtn').addEventListener('click',
  () => devSeed('bulk', 'a large synthetic reserve (~55 tigers)'));
$('#seedDemoBtn').addEventListener('click',
  () => devSeed('demo', 'the small spec-demo reserve (13 tigers)'));
$('#seedBlankBtn').addEventListener('click',
  () => devSeed('blank', 'an empty reserve'));

async function restoreLiveData() {
  if (!confirm('Show your live data again, in place of the demo that\'s currently active? '
    + 'The demo data itself is not deleted -- you can load it again later.')) return;
  $$('#v-ops .toolbar button').forEach((b) => { b.disabled = true; });
  $('#seedMsg').textContent = 'Restoring…';
  try {
    await api('/api/dev/restore-live', { method: 'POST' });
    location.reload();
  } catch (e) {
    $('#seedMsg').textContent = `Failed: ${e.detail || e.message}`;
    $$('#v-ops .toolbar button').forEach((b) => { b.disabled = false; });
  }
}
$('#restoreLiveBtn').addEventListener('click', restoreLiveData);

async function renderSeedModeStatus() {
  const banner = $('#seedModeBanner');
  const label = $('#seedModeLabel');
  const note = $('#seedModeNote');
  const restoreBtn = $('#restoreLiveBtn');
  if (!banner) return;
  try {
    const d = await api('/api/dev/data-mode');
    // Tokens, not hex: this banner is the one element on screen whose whole
    // job is telling you which dataset you are looking at mid-presentation,
    // so it has to stay legible in both themes.
    const seeded = d.mode === 'seeded';
    banner.style.background = seeded ? 'var(--warn-bg)' : 'var(--ok-bg)';
    banner.style.borderColor = seeded ? 'var(--warn-line)' : 'var(--ok-line)';
    label.style.color = seeded ? 'var(--warn-fg)' : 'var(--ok-fg)';
    label.textContent = seeded ? 'Showing: Demo Data' : 'Showing: Your Live Data';
    note.textContent = seeded
      ? (d.live_backup_available
          ? 'Your own data is saved and hidden right now -- click "Show my live data" to bring it back.'
          : 'No live data has been saved yet (nothing was active before this demo was loaded).')
      : (d.live_backup_available
          ? 'A saved demo-time snapshot also exists if you need to restore it again.'
          : 'This is the data you have fed in yourself.');
    if (restoreBtn) restoreBtn.disabled = !d.live_backup_available;
  } catch (e) {
    label.textContent = 'Could not check current data mode';
    note.textContent = e.detail || e.message || '';
  }
}

/* ── sync ──────────────────────────────────────────────────────────────── */
RENDER.sync = async () => {
  const d = await api(`/api/sync/status?reserve_id=${S.reserve.reserve_id}`);
  let keyInfo = { sync_secret: '', key_length: 0 };
  try {
    keyInfo = await api('/api/sync/key');
  } catch (e) {
    /* non-admin roles might not fetch key details */
  }

  const canBundle = d.bundle_sync_enabled;
  const sec = keyInfo.sync_secret || '';
  const maskedSec = sec ? (sec.slice(0, 6) + '••••••••••••••••••••••••' + sec.slice(-4)) : '—';

  $('#syncBody').innerHTML = `
    <!-- Top Overview & Actions Card -->
    <div class="card pad">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:var(--s2)">
        <div>
          <h2>Offline Sync Between Laptops</h2>
          <p class="note" style="margin-top:var(--s1)">
            Air-gapped edge-to-edge synchronization across range-office laptops without internet or central servers.
          </p>
        </div>
        <div>
          ${canBundle 
            ? '<span class="tag ok" style="font-weight:600;padding:4px 10px">● Bundle Sync Ready</span>'
            : '<span class="badge" style="background:var(--bad-bg);color:#c5221f;font-weight:600;padding:4px 10px;border-radius:4px;border:1px solid #fad2cf">● Sync Disabled</span>'}
        </div>
      </div>

      <dl class="kv" style="margin-top:var(--s3)">
        <dt>Local Node ID</dt><dd><code class="num" style="font-size:13px">${esc(d.node_id)}</code></dd>
        <dt>Sync Status</dt><dd>${canBundle ? 'Active (Cryptographically Signed)' : 'Off (' + esc(d.bundle_sync_reason || '') + ')'}</dd>
        <dt>Unsynced Local Changes</dt><dd class="num">${d.pending_rows != null ? d.pending_rows + ' rows waiting' : '0 rows (up to date)'}</dd>
      </dl>

      <div class="hr"></div>

      <!-- Action Buttons -->
      <div style="display:flex;flex-direction:column;gap:var(--s2)">
        <h3 style="font-size:14px;color:var(--text);text-transform:uppercase;letter-spacing:0.5px">Data Actions</h3>
        <div class="toolbar" style="margin-top:2px">
          ${canBundle
            ? `<a class="btn primary" href="/api/sync/bundle?reserve_id=${S.reserve.reserve_id}" download>Write bundle to drive</a>`
            : '<button disabled>Write bundle to drive</button>'}
          <input type="file" id="syncApplyFile" accept="application/json,.json,.pugmark-bundle.json" hidden>
          <button id="syncApplyBtn" ${canBundle ? '' : 'disabled'}>Apply a bundle</button>
          <span class="note" id="syncApplyNote" style="font-weight:500"></span>
        </div>
      </div>
    </div>

    <!-- Visual How-It-Works Guide Card -->
    <div class="card pad" style="margin-top:var(--s4);background:var(--surface)">
      <h2 style="margin-bottom:var(--s2)">How Range-Office Sharing Works (3 Simple Steps)</h2>
      <p class="note" style="margin-bottom:var(--s3)">
        Forest ranges operate completely offline. Follow this 3-step workflow to share camera trap data and tiger sightings between range laptops:
      </p>

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));gap:var(--s3)">
        <!-- Step 1 -->
        <div style="background:var(--bg);padding:var(--s3);border-radius:6px;border:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:var(--s2);margin-bottom:var(--s2)">
            <span style="background:var(--pelage);color:#fff;font-weight:bold;width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px">1</span>
            <strong style="font-size:14px">Export (Outbound)</strong>
          </div>
          <p style="font-size:13px;color:var(--text);line-height:1.5;margin:0">
            On <strong>Laptop A</strong> (e.g. Sillari Range), click <strong>Write bundle to drive</strong>. This packages all new photo runs, tiger IDs, and station records into a single cryptographically signed file (<code>.pugmark-bundle.json</code>).
          </p>
        </div>

        <!-- Step 2 -->
        <div style="background:var(--bg);padding:var(--s3);border-radius:6px;border:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:var(--s2);margin-bottom:var(--s2)">
            <span style="background:var(--pelage);color:#fff;font-weight:bold;width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px">2</span>
            <strong style="font-size:14px">Transfer (USB / SD)</strong>
          </div>
          <p style="font-size:13px;color:var(--text);line-height:1.5;margin:0">
            Save or copy that bundle file onto any standard USB drive or SD card. Take the USB stick to another range office or division HQ during review meetings. No network or internet needed.
          </p>
        </div>

        <!-- Step 3 -->
        <div style="background:var(--bg);padding:var(--s3);border-radius:6px;border:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:var(--s2);margin-bottom:var(--s2)">
            <span style="background:var(--pelage);color:#fff;font-weight:bold;width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px">3</span>
            <strong style="font-size:14px">Import (Inbound)</strong>
          </div>
          <p style="font-size:13px;color:var(--text);line-height:1.5;margin:0">
            On <strong>Laptop B</strong> (e.g. Khursapar Range), click <strong>Apply a bundle</strong> and select the file from the USB drive. Pugmark verifies the signature and merges the sightings seamlessly without duplicate IDs.
          </p>
        </div>
      </div>
    </div>

    <!-- Sync Secret & Laptop Pairing Manager Card -->
    <div class="card pad" style="margin-top:var(--s4)">
      <h2>Laptop Pairing & Shared Sync Key</h2>
      <p class="note" style="margin-top:var(--s1);margin-bottom:var(--s3)">
        To prevent unauthorized data injection, all laptops belonging to the same tiger reserve must share the same <strong>Sync Secret Key</strong>.
      </p>

      <div style="background:var(--bg);padding:var(--s3);border-radius:6px;border:1px solid var(--border)">
        <dl class="kv">
          <dt>Shared Key (HMAC)</dt>
          <dd style="display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap">
            <code id="syncKeyDisplay" class="num" style="font-size:13px;background:var(--surface);padding:4px 8px;border-radius:4px">${esc(maskedSec)}</code>
            <button type="button" id="toggleKeyRevealBtn" style="font-size:12px;padding:3px 8px">Show</button>
            <button type="button" id="copyKeyBtn" style="font-size:12px;padding:3px 8px">Copy</button>
          </dd>
        </dl>

        <div class="toolbar" style="margin-top:var(--s3);border-top:1px solid var(--border);padding-top:var(--s3)">
          <a class="btn" href="/api/sync/key/download" download style="font-size:13px">Download key file (sync_secret.txt)</a>
          <button type="button" id="showPairModalBtn" style="font-size:13px">Pair / Paste key from another laptop</button>
          <button type="button" id="genNewKeyBtn" style="font-size:13px">Generate new key</button>
        </div>
        <div id="keyActionMsg" style="margin-top:var(--s2);font-size:13px;color:var(--pelage)"></div>
      </div>

      <!-- Quick pairing instructions -->
      <div style="margin-top:var(--s3);font-size:13px;color:var(--text);line-height:1.6">
        <strong>How to pair two laptops:</strong>
        <ol style="margin:var(--s1) 0 0 var(--s4)">
          <li>On Laptop 1: Click <strong>Download key file (sync_secret.txt)</strong> and put it on a USB drive.</li>
          <li>On Laptop 2: Click <strong>Pair / Paste key from another laptop</strong> and paste the key (or place the file in the <code>data/</code> folder).</li>
          <li>Both laptops are now paired and can exchange signed bundles anytime!</li>
        </ol>
      </div>
    </div>`;

  // Attach handlers
  let revealed = false;
  $('#toggleKeyRevealBtn')?.addEventListener('click', () => {
    revealed = !revealed;
    $('#syncKeyDisplay').textContent = revealed ? sec : maskedSec;
    $('#toggleKeyRevealBtn').textContent = revealed ? 'Hide' : 'Show';
  });

  $('#copyKeyBtn')?.addEventListener('click', async () => {
    if (!sec) return;
    try {
      await navigator.clipboard.writeText(sec);
      $('#keyActionMsg').textContent = 'Key copied to clipboard!';
      setTimeout(() => { $('#keyActionMsg').textContent = ''; }, 3000);
    } catch (e) {
      prompt('Copy your Sync Secret Key:', sec);
    }
  });

  $('#genNewKeyBtn')?.addEventListener('click', async () => {
    if (!confirm('Generating a new key will require copying the new key to your other laptops so they can continue syncing. Are you sure?')) return;
    try {
      const res = await api('/api/sync/key', { method: 'POST', body: {} });
      alert('New sync key generated and saved!');
      RENDER.sync();
    } catch (e) {
      alert('Failed to generate key: ' + (e.detail || e.message));
    }
  });

  $('#showPairModalBtn')?.addEventListener('click', async () => {
    const inputKey = prompt('Paste the Sync Secret Key from your other laptop (or leave blank to cancel):');
    if (!inputKey || !inputKey.trim()) return;
    try {
      await api('/api/sync/key', { method: 'POST', body: { new_secret: inputKey.trim() } });
      alert('Sync key successfully updated! This laptop is now paired.');
      RENDER.sync();
    } catch (e) {
      alert('Failed to save key: ' + (e.detail || e.message));
    }
  });

  $('#syncApplyBtn')?.addEventListener('click', () => $('#syncApplyFile').click());
  $('#syncApplyFile')?.addEventListener('change', async () => {
    const file = $('#syncApplyFile').files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    $('#syncApplyNote').textContent = 'Verifying and applying bundle…';
    try {
      const r = await fetch('/api/sync/bundle/apply', { method: 'POST', body: fd });
      const stats = await r.json();
      if (!r.ok) throw new Error(stats.detail || 'apply failed');
      $('#syncApplyNote').textContent =
        `Success: ${nf(stats.inserted)} new rows added, ${nf(stats.unchanged)} already present, `
        + `${nf(stats.conflict_resolved)} resolved by timestamp.`;
      setTimeout(() => { RENDER.sync(); }, 2500);
    } catch (e) {
      $('#syncApplyNote').textContent = 'Error: ' + e.message;
    }
  });
};

/* ── users (admin only) ────────────────────────────────────────────────── */
RENDER.users = async function() {
  if (S.user?.role !== 'admin') {
    location.hash = '#run';
    return;
  }
  const users = await api('/api/auth/users');
  const cols = ['Username', 'Display Name', 'Role', 'Status', 'Password Set', 'Failed Attempts', 'Last Login', 'Actions'];
  const rows = users.map((u) => {
    const isSelf = u.username === S.user.username;
    const statusHtml = u.disabled
      ? '<span style="color:var(--act);font-weight:600">Disabled</span>'
      : (u.locked_until && u.locked_until > new Date().toISOString()
        ? `<span style="color:var(--watch);font-weight:600">Locked (${esc(u.locked_until)})</span>`
        : '<span style="color:var(--info);font-weight:600">Active</span>');
    const pwdState = u.must_change_password
      ? '<span style="color:var(--watch)">Temporary</span>'
      : '<span>Set</span>';
    
    let actionButtons = '';
    if (isSelf) {
      actionButtons = '<span style="color:var(--muted);font-size:12px">Current session</span>';
    } else if (u.disabled) {
      actionButtons = `
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <button type="button" class="small enable-user-btn" data-username="${esc(u.username)}" style="background:#2b5a44;color:#fff;border:0;padding:3px 8px;font-size:11px">Enable</button>
          <button type="button" class="small reset-user-btn" data-username="${esc(u.username)}" style="padding:3px 8px;font-size:11px">Reset Pass</button>
          <button type="button" class="danger small delete-user-btn" data-username="${esc(u.username)}" style="padding:3px 8px;font-size:11px">Delete</button>
        </div>`;
    } else {
      actionButtons = `
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <button type="button" class="small reset-user-btn" data-username="${esc(u.username)}" style="padding:3px 8px;font-size:11px">Reset Pass</button>
          <button type="button" class="danger small disable-user-btn" data-username="${esc(u.username)}" style="padding:3px 8px;font-size:11px">Disable</button>
        </div>`;
    }

    return [
      `<td class="num"><b>${esc(u.username)}</b>${isSelf ? ' (you)' : ''}</td>`,
      `<td>${esc(u.display_name || '—')}</td>`,
      `<td><span class="user-role">${esc(u.role)}</span></td>`,
      `<td>${statusHtml}</td>`,
      `<td>${pwdState}</td>`,
      `<td class="num">${u.failed_login_attempts || 0}</td>`,
      `<td class="num">${u.last_login_at ? esc(u.last_login_at.replace('T', ' ')) : 'Never'}</td>`,
      `<td>${actionButtons}</td>`,
    ];
  });
  $('#usersTable').innerHTML = table(cols, rows);

  // Wire Disable Action
  $$('.disable-user-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const uname = btn.dataset.username;
      if (!confirm(`Disable user account '${uname}'? They will be locked out until re-enabled.`)) return;
      try {
        await api(`/api/auth/users/${encodeURIComponent(uname)}/disable`, { method: 'POST' });
        RENDER.users();
      } catch (e) {
        alert(e.message);
      }
    });
  });

  // Wire Enable Action
  $$('.enable-user-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const uname = btn.dataset.username;
      try {
        await api(`/api/auth/users/${encodeURIComponent(uname)}/enable`, { method: 'POST' });
        RENDER.users();
      } catch (e) {
        alert(e.message);
      }
    });
  });

  // Wire Delete Action
  $$('.delete-user-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const uname = btn.dataset.username;
      if (!confirm(`Permanently delete user '${uname}' from the system? This action cannot be undone.`)) return;
      try {
        await api(`/api/auth/users/${encodeURIComponent(uname)}/delete`, { method: 'POST' });
        RENDER.users();
      } catch (e) {
        alert(e.message);
      }
    });
  });

  // Wire Reset Credentials Action
  $$('.reset-user-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const uname = btn.dataset.username;
      if (!confirm(`Generate a new temporary password and recovery code for '${uname}'? Any active sessions will be terminated.`)) return;
      try {
        const res = await api(`/api/auth/users/${encodeURIComponent(uname)}/reset-credentials`, { method: 'POST' });
        const callout = $('#userCreatedCallout');
        const newBody = $('#newUserBody');
        if (callout) {
          if (newBody) newBody.hidden = false;
          callout.innerHTML = `
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:var(--s3)">
              <span style="font-size:20px">🔑</span>
              <h3 style="margin:0;color:var(--pelage)">Credentials Reset: ${esc(res.username)}</h3>
            </div>
            <p style="margin:0 0 var(--s3);padding:10px 14px;background:var(--warn-bg);border:1px solid var(--warn-line);border-radius:6px;font-size:13px;line-height:1.5;color:var(--warn-fg)">
              <strong>⚠ IMPORTANT:</strong> The old password and recovery code are void. Hand these new credentials to the officer. They must change their password on first login.
            </p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--s3);margin-bottom:var(--s3)">
              <div style="background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:14px">
                <div style="font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--muted);margin-bottom:8px">NEW TEMPORARY PASSWORD</div>
                <code style="font-family:var(--f-data);font-size:16px;font-weight:700;color:var(--ink);display:block;letter-spacing:.06em">${esc(res.temp_password)}</code>
              </div>
              <div style="background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:14px">
                <div style="font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--muted);margin-bottom:8px">NEW RECOVERY CODE</div>
                <code style="font-family:var(--f-data);font-size:13px;font-weight:700;color:var(--ink);display:block;word-break:break-all;letter-spacing:.04em">${esc(res.recovery_code)}</code>
              </div>
            </div>
            <button type="button" class="primary" id="dismissResetCalloutBtn">✓ Done — I have handed over these credentials</button>
          `;
          callout.hidden = false;
          callout.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          $('#dismissResetCalloutBtn').onclick = () => {
            callout.hidden = true;
            RENDER.users();
          };
        }
      } catch (e) {
        alert(e.message);
      }
    });
  });

  const newToggle = $('#newUserToggle');
  const newBody = $('#newUserBody');
  if (newToggle && newBody) {
    newToggle.onclick = () => { newBody.hidden = !newBody.hidden; };
    $('#cancelCreateUserBtn').onclick = () => { newBody.hidden = true; };
  }

  $('#createUserForm').onsubmit = async () => {
    const username = $('#newUsername').value.trim();
    const display_name = $('#newDisplayName').value.trim() || null;
    const role = $('#newRole').value;
    const errEl = $('#createUserError');
    const callout = $('#userCreatedCallout');
    errEl.textContent = '';
    callout.hidden = true;
    if (!username) { errEl.textContent = 'Username is required.'; return; }
    try {
      const res = await api('/api/auth/users', {
        method: 'POST',
        body: { username, display_name, role }
      });
      callout.innerHTML = `
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:var(--s3)">
          <span style="font-size:20px">✅</span>
          <h3 style="margin:0;color:var(--pelage)">Account Created: ${esc(res.username)}</h3>
        </div>
        <p style="margin:0 0 var(--s3);padding:10px 14px;background:var(--warn-bg);border:1px solid var(--warn-line);border-radius:6px;font-size:13px;line-height:1.5;color:var(--warn-fg)">
          <strong>⚠ IMPORTANT:</strong> These credentials are shown <strong>exactly once</strong>. Write them down or print and hand to the officer immediately.
        </p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--s3);margin-bottom:var(--s3)">
          <div style="background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:14px">
            <div style="font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--muted);margin-bottom:8px">TEMPORARY PASSWORD</div>
            <code style="font-family:var(--f-data);font-size:16px;font-weight:700;color:var(--ink);display:block;letter-spacing:.06em">${esc(res.temp_password)}</code>
          </div>
          <div style="background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:14px">
            <div style="font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--muted);margin-bottom:8px">RECOVERY CODE</div>
            <code style="font-family:var(--f-data);font-size:13px;font-weight:700;color:var(--ink);display:block;word-break:break-all;letter-spacing:.04em">${esc(res.recovery_code)}</code>
          </div>
        </div>
        <button type="button" class="primary" id="dismissCalloutBtn">✓ Done — I have recorded these credentials</button>
      `;
      callout.hidden = false;
      callout.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      $('#dismissCalloutBtn').onclick = () => {
        callout.hidden = true;
        newBody.hidden = true;
        $('#createUserForm').reset();
        RENDER.users();
      };
    } catch (e) {
      errEl.textContent = e.detail || e.message;
    }
  };
};

/* ── auth controller ───────────────────────────────────────────────────── */
let lockoutTimer = null;

/* ── the sign-in stage ───────────────────────────────────────────────────
   The reserve photograph is the screen; the credentials card is summoned
   over it with a double-click. Two rules keep that from becoming a trap:

     * 'login' is the only mode that starts closed. 'change' (a forced
       password change) and 'forgot' are states the operator did not choose
       and cannot leave, so the card opens immediately -- making somebody
       double-click an image to reach a mandatory step would be a puzzle,
       not a flourish.
     * Once open it STAYS open across re-renders. A failed login calls back
       into this screen, and a card that closed on every wrong password
       would hide the error explaining what went wrong.                    */
function openAuthCard(focus = true) {
  const modal = $('#authModal');
  if (!modal || !modal.hidden) return;
  modal.hidden = false;
  $('#authStage')?.classList.add('is-open');
  if (focus) {
    // first empty field, so a forced password change does not land the
    // cursor on a username the operator cannot edit anyway
    const field = modal.querySelector(
      'input:not([type=hidden]):not([disabled])');
    setTimeout(() => field?.focus(), 220);
  }
}

function closeAuthCard() {
  const modal = $('#authModal');
  if (!modal) return;
  modal.hidden = true;
  $('#authStage')?.classList.remove('is-open');
}

function authStageInit() {
  const stage = $('#authStage');
  if (!stage) return;
  stage.addEventListener('dblclick', () => openAuthCard());
  // Keyboard equivalent: a double-click is not reachable without a mouse,
  // and this is the only door into the application.
  stage.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openAuthCard(); }
  });
  // Touch has no reliable double-click -- a double-tap is a zoom gesture on
  // most mobile browsers, so on a coarse pointer a single tap opens it.
  if (window.matchMedia?.('(pointer: coarse)').matches) {
    stage.addEventListener('click', () => openAuthCard());
  }
  $('#authModalClose')?.addEventListener('click', closeAuthCard);
  $('#authModal')?.addEventListener('click', (e) => {
    if (e.target === $('#authModal')) closeAuthCard();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const modal = $('#authModal');
    if (!modal || modal.hidden) return;
    // Escape must not strand someone on a step they are required to
    // finish, so only the ordinary login card is dismissible -- a forced
    // password change or a recovery flow stays put.
    if ($('#authLoginPanel')?.hidden === false) closeAuthCard();
  });
}

function showAuth(mode = 'login') {
  const shell = $('#authShell');
  if (!shell) return;
  shell.hidden = false;
  shell.style.display = 'flex';
  if (mode === 'login') {
    // leave an already-open card open (see the note above about errors)
    if ($('#authModal')?.hidden !== false) closeAuthCard();
  } else {
    openAuthCard();
  }
  $('#authLoginPanel').hidden = (mode !== 'login');
  $('#authForgotPanel').hidden = (mode !== 'forgot');
  $('#authChangePanel').hidden = (mode !== 'change');
  $('#loginError').hidden = true;
  if ($('#loginAttemptsBadge')) $('#loginAttemptsBadge').hidden = true;
  if ($('#loginLockout')) $('#loginLockout').hidden = true;
  $('#forgotError').hidden = true;
  $('#forgotSuccess').hidden = true;
  $('#changeError').hidden = true;
}

function hideAuth() {
  const shell = $('#authShell');
  if (shell) {
    shell.hidden = true;
    shell.style.display = 'none';
  }
  if (lockoutTimer) {
    clearInterval(lockoutTimer);
    lockoutTimer = null;
  }
}

function updateUserUI() {
  const pill = $('#userPill');
  const navUsers = $('#navUsers');
  // Log out now sits outside the user pill (far right of the topbar), so it
  // needs its own visibility -- it used to inherit the pill's hidden state.
  const logout = $('#logoutBtn');
  if (!S.user) {
    if (pill) pill.hidden = true;
    if (logout) logout.hidden = true;
    if (navUsers) navUsers.hidden = true;
    return;
  }
  if (logout) logout.hidden = false;
  if (pill) {
    pill.hidden = false;
    $('#userRoleBadge').textContent = S.user.role;
    $('#userNameLabel').textContent = S.user.display_name || S.user.username;
  }
  if (navUsers) {
    navUsers.hidden = (S.user.role !== 'admin');
  }
  if ($('#idActor')) {
    $('#idActor').value = S.user.username;
  }
}

function startLockoutCountdown(seconds) {
  if (lockoutTimer) clearInterval(lockoutTimer);
  const lockoutEl = $('#loginLockout');
  const submitBtn = $('#loginSubmitBtn');
  const attemptsEl = $('#loginAttemptsBadge');
  const errorEl = $('#loginError');

  if (attemptsEl) attemptsEl.hidden = true;
  if (errorEl) errorEl.hidden = true;
  if (submitBtn) submitBtn.disabled = true;

  let remaining = seconds;
  const updateUI = () => {
    if (remaining <= 0) {
      clearInterval(lockoutTimer);
      lockoutTimer = null;
      if (lockoutEl) {
        lockoutEl.className = 'auth-attempts';
        lockoutEl.innerHTML = `<strong>Lockout expired.</strong> You may now attempt to sign in.`;
      }
      if (submitBtn) submitBtn.disabled = false;
      return;
    }
    if (lockoutEl) {
      lockoutEl.className = 'auth-lockout';
      lockoutEl.hidden = false;
      lockoutEl.innerHTML = `<strong>Account Temporarily Locked</strong><br>
        Too many failed attempts. Try again in <span class="lockout-timer">${remaining}s</span> (or reset via recovery code).`;
    }
    remaining--;
  };
  updateUI();
  lockoutTimer = setInterval(updateUI, 1000);
}

function setupAuth() {
  $$('.pwd-toggle-btn').forEach((btn) => {
    // The icon lives in its own span so the CSS flip animation has an
    // element to run on -- re-setting textContent on the button itself
    // would restart nothing and animate nothing.
    // Line-art SVG rather than emoji: 👁/🙈 render as full-colour pictures
    // that vary by platform font and sit oddly against a monochrome form.
    // These inherit currentColor, so they theme themselves.
    const EYE = '<svg viewBox="0 0 20 20" aria-hidden="true">'
      + '<path d="M1.8 10S4.9 4.6 10 4.6 18.2 10 18.2 10 15.1 15.4 10 15.4 1.8 10 1.8 10z"/>'
      + '<circle cx="10" cy="10" r="2.6"/></svg>';
    const EYE_OFF = '<svg viewBox="0 0 20 20" aria-hidden="true">'
      + '<path d="M8.2 5a7.6 7.6 0 0 1 1.8-.2c5.1 0 8.2 5.2 8.2 5.2a15 15 0 0 1-2.4 2.9M5.6 6.3A14.6 14.6 0 0 0 1.8 10S4.9 15.2 10 15.2a7.7 7.7 0 0 0 3-.6"/>'
      + '<path d="M8.3 8.4a2.6 2.6 0 0 0 3.5 3.5"/><path d="M3 3l14 14"/></svg>';

    const paint = (revealed, animate = true) => {
      // animate=false on first paint: three icons flipping in unison the
      // moment the login screen appears reads as a rendering glitch, not
      // as feedback. The flip should only ever answer a click.
      btn.innerHTML = `<span class="pw-icon${animate ? '' : ' no-anim'}">${revealed ? EYE_OFF : EYE}</span>`;
      btn.classList.toggle('revealed', revealed);
      btn.setAttribute('aria-pressed', String(revealed));
      btn.title = revealed ? 'Hide password' : 'Show password';
      btn.setAttribute('aria-label', btn.title);
    };
    paint(false, false);

    btn.onclick = (e) => {
      e.preventDefault();
      // querySelector rather than previousElementSibling: the markup now
      // has the icon span inside the button, and a future wrapper change
      // should not silently break this again.
      const input = btn.parentElement?.querySelector('input');
      if (!input) return;
      const revealed = input.type === 'password';
      input.type = revealed ? 'text' : 'password';
      paint(revealed);
      // Keep the caret where the operator left it -- retyping a long
      // password because the cursor jumped to the start is exactly the
      // friction this button exists to remove.
      const pos = input.value.length;
      try { input.setSelectionRange(pos, pos); } catch (err) { /* type=text only */ }
      input.focus();
    };
  });

  $('#toForgotBtn')?.addEventListener('click', (e) => {
    e.preventDefault();
    showAuth('forgot');
  });
  $('#toLoginBtn')?.addEventListener('click', (e) => {
    e.preventDefault();
    showAuth('login');
  });
  $('#logoutBtn')?.addEventListener('click', async () => {
    try {
      await api('/api/auth/logout', { method: 'POST' });
    } catch { /* ignore */ }
    S.user = null;
    updateUserUI();
    showAuth('login');
  });

  $('#loginForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('#loginUsername').value.trim();
    const password = $('#loginPassword').value;
    const errEl = $('#loginError');
    const attemptsEl = $('#loginAttemptsBadge');
    const lockoutEl = $('#loginLockout');
    errEl.hidden = true;
    if (attemptsEl) attemptsEl.hidden = true;
    if (lockoutEl) lockoutEl.hidden = true;

    try {
      const user = await api('/api/auth/login', {
        method: 'POST',
        body: { username, password }
      });
      S.user = user;
      updateUserUI();
      if (user.must_change_password) {
        if ($('#changeCurrentPassword')) $('#changeCurrentPassword').value = password;
        showAuth('change');
      } else {
        hideAuth();
        await initAppSafely();
      }
    } catch (err) {
      const data = err.data || {};
      if (data.locked && data.lockout_seconds) {
        startLockoutCountdown(data.lockout_seconds);
      } else {
        if (typeof data.attempts_remaining === 'number') {
          const rem = data.attempts_remaining;
          const max = data.max_attempts || 5;
          if (attemptsEl) {
            attemptsEl.textContent = `${rem} of ${max} attempt${rem !== 1 ? 's' : ''} remaining`;
            attemptsEl.className = `auth-attempts ${rem <= 2 ? 'danger' : ''}`;
            attemptsEl.hidden = false;
          }
        }
        errEl.textContent = err.detail || 'Invalid username or password.';
        errEl.hidden = false;
      }
    }
  });

  $('#forgotForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('#forgotUsername').value.trim();
    const recovery_code = $('#forgotCode').value.trim();
    const new_password = $('#forgotNewPassword').value;
    const errEl = $('#forgotError');
    const succEl = $('#forgotSuccess');
    errEl.hidden = true;
    succEl.hidden = true;
    if (new_password.length < 12) {
      errEl.textContent = 'New password must be at least 12 characters.';
      errEl.hidden = false;
      return;
    }
    try {
      const res = await api('/api/auth/forgot-password', {
        method: 'POST',
        body: { username, recovery_code, new_password }
      });
      succEl.innerHTML = `<strong>Password reset successful.</strong><br>
        Your NEW recovery code is: <code class="num" style="font-size:13px;display:block;margin:6px 0;padding:4px;background:var(--surface)">${esc(res.recovery_code)}</code>
        Please record this code securely. You may now sign in with your new password.`;
      succEl.hidden = false;
      $('#forgotForm').reset();
      if ($('#loginUsername')) $('#loginUsername').value = username;
    } catch (err) {
      errEl.textContent = err.detail || 'Invalid username or recovery code.';
      errEl.hidden = false;
    }
  });

  $('#changeForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const current_password = $('#changeCurrentPassword').value;
    const new_password = $('#changeNewPassword').value;
    const confirm_password = $('#changeConfirmPassword').value;
    const errEl = $('#changeError');
    errEl.hidden = true;
    if (new_password.length < 12) {
      errEl.textContent = 'New password must be at least 12 characters.';
      errEl.hidden = false;
      return;
    }
    if (new_password !== confirm_password) {
      errEl.textContent = 'New passwords do not match.';
      errEl.hidden = false;
      return;
    }
    try {
      const res = await api('/api/auth/change-password', {
        method: 'POST',
        body: { current_password, new_password }
      });
      if (res.relogin_required) {
        alert('Password changed successfully. Please sign in with your new password.');
        S.user = null;
        updateUserUI();
        $('#changeForm').reset();
        showAuth('login');
      } else {
        S.user = res;
        updateUserUI();
        $('#changeForm').reset();
        hideAuth();
        await initAppSafely();
      }
    } catch (err) {
      errEl.textContent = err.detail || 'Failed to change password.';
      errEl.hidden = false;
    }
  });
}

/* ── STATIONS ─────────────────────────────────────────────────────────────
   Full CRUD for the physical camera trap grid.
   Uses /api/stations (GET/POST/PUT/DELETE) and /api/stations/import-csv
   and /api/stations/import-geojson endpoints. */

let _stationsCache = [];

function _stationStatusPill(status) {
  if (!status || status === 'active') return '<span style="color:#059669;font-weight:600;font-size:11px">● ACTIVE</span>';
  if (status === 'offline')            return '<span style="color:#9ca3af;font-weight:600;font-size:11px">● OFFLINE</span>';
  return `<span style="color:var(--ink-muted);font-size:11px">${esc(status.toUpperCase())}</span>`;
}

function _stationZoneChip(zone) {
  const map = { core: '#d97706', buffer: '#0891b2', corridor: '#7c3aed' };
  const col = map[zone] || '#6b7280';
  return `<span style="display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;font-weight:600;color:#fff;background:${col}">${esc((zone||'?').toUpperCase())}</span>`;
}

function _renderStationsTable(rows) {
  if (!rows.length) {
    return `<tr><td colspan="9" class="empty" style="padding:32px;text-align:center">
      No stations match your filter. Click <strong>+ Add Station</strong> or Import to register your first camera trap.</td></tr>`;
  }
  return rows.map(s => {
    const lastSeen  = s.last_image_at ? s.last_image_at.slice(0, 10) : '—';
    const imgCount  = (s.image_count != null) ? nf(s.image_count) : '—';
    const deployDay = s.active_from ? s.active_from.slice(0, 10) : '—';
    const hardware  = [s.camera_make, s.camera_model].filter(Boolean).join(' ') || '—';
    const coords    = (s.lat != null && s.lon != null)
      ? `${(+s.lat).toFixed(4)}, ${(+s.lon).toFixed(4)}`
      : '—';
    return `<tr style="cursor:pointer" data-sid="${esc(s.station_id)}" class="station-row">
      <td style="font-family:monospace;font-size:12px">${esc(s.station_id)}</td>
      <td><strong>${esc(s.name || s.station_id)}</strong></td>
      <td>${_stationZoneChip(s.zone)}</td>
      <td>${_stationStatusPill(s.status)}</td>
      <td style="font-size:12px;color:var(--ink-muted)">${coords}</td>
      <td style="font-size:12px">${hardware}</td>
      <td style="font-size:12px;color:var(--ink-muted)">${deployDay}</td>
      <td class="num">${imgCount}</td>
      <td style="font-size:12px;color:var(--ink-muted)">${lastSeen}</td>
    </tr>`;
  }).join('');
}

function _stationsApplyFilter() {
  const q      = ($('#stationSearchInput')?.value  || '').toLowerCase();
  const zone   = $('#stationFilterZone')?.value    || '';
  const status = $('#stationFilterStatus')?.value  || '';
  let rows = _stationsCache;
  if (q)      rows = rows.filter(s => (s.station_id+' '+(s.name||'')+' '+(s.camera_make||'')+' '+(s.camera_model||'')).toLowerCase().includes(q));
  if (zone)   rows = rows.filter(s => s.zone === zone);
  if (status) rows = rows.filter(s => (s.status || 'active') === status);

  const tbody = $('#stationsTable tbody');
  if (tbody) tbody.innerHTML = _renderStationsTable(rows);
  $$('.station-row').forEach(tr => {
    tr.addEventListener('click', () => _openStationModal('edit', _stationsCache.find(s => s.station_id === tr.dataset.sid)));
  });
}

function _openStationModal(mode, station = null) {
  const modal = $('#stationModal');
  if (!modal) return;
  $('#stationModalTitle').textContent  = mode === 'edit' ? `Edit Station — ${station?.station_id || ''}` : 'Add Camera Station';
  $('#stationFormMode').value          = mode;
  $('#stationFormOriginalId').value    = station?.station_id || '';
  $('#stationFormId').value            = station?.station_id || '';
  $('#stationFormId').readOnly         = mode === 'edit';
  $('#stationFormName').value          = station?.name || '';
  $('#stationFormLat').value           = station?.lat  ?? '';
  $('#stationFormLon').value           = station?.lon  ?? '';
  $('#stationFormZone').value          = station?.zone || 'core';
  $('#stationFormVillageDist').value   = station?.village_dist_km ?? 4.5;
  $('#stationFormMake').value          = station?.camera_make  || '';
  $('#stationFormModel').value         = station?.camera_model || '';
  $('#stationFormSerial').value        = station?.camera_serial || '';
  $('#stationFormHint').value          = station?.folder_hint  || '';
  $('#stationFormStatus').value        = station?.status || 'active';
  $('#stationFormDelete').hidden       = mode !== 'edit';
  $('#stationFormError').hidden        = true;
  $('#stationFormError').textContent   = '';
  modal.hidden = false;
}

function _closeStationModal() {
  const modal = $('#stationModal');
  if (modal) modal.hidden = true;
}

async function _submitStationForm(e) {
  e?.preventDefault();
  const mode = $('#stationFormMode').value;
  const err  = $('#stationFormError');
  err.hidden = true;

  const data = {
    station_id:      $('#stationFormId').value.trim(),
    name:            $('#stationFormName').value.trim(),
    lat:             parseFloat($('#stationFormLat').value),
    lon:             parseFloat($('#stationFormLon').value),
    zone:            $('#stationFormZone').value,
    village_dist_km: parseFloat($('#stationFormVillageDist').value) || 5.0,
    camera_make:     $('#stationFormMake').value.trim()   || null,
    camera_model:    $('#stationFormModel').value.trim()  || null,
    camera_serial:   $('#stationFormSerial').value.trim() || null,
    folder_hint:     $('#stationFormHint').value.trim()   || null,
    status:          $('#stationFormStatus').value,
  };

  if (!data.station_id) { err.textContent = 'Station ID is required.'; err.hidden = false; return; }
  if (isNaN(data.lat) || isNaN(data.lon)) { err.textContent = 'Valid latitude and longitude are required.'; err.hidden = false; return; }

  const rid = S.reserve?.reserve_id;
  try {
    if (mode === 'create') {
      await api(`/api/reserves/${encodeURIComponent(rid)}/stations`, { method: 'POST', body: data });
    } else {
      const sid = $('#stationFormOriginalId').value;
      await api(`/api/reserves/${encodeURIComponent(rid)}/stations/${encodeURIComponent(sid)}`, { method: 'PUT', body: data });
    }
    _closeStationModal();
    await RENDER.stations();
  } catch (ex) {
    err.textContent = ex.detail || ex.message || 'Failed to save station.';
    err.hidden = false;
  }
}

async function _deleteStation() {
  const sid = $('#stationFormOriginalId').value;
  if (!sid) return;
  if (!confirm(`Delete station ${sid}?\n\nThis cannot be undone if images are attached to it.`)) return;
  const err = $('#stationFormError');
  err.hidden = true;
  const rid = S.reserve?.reserve_id;
  try {
    await api(`/api/reserves/${encodeURIComponent(rid)}/stations/${encodeURIComponent(sid)}`, { method: 'DELETE' });
    _closeStationModal();
    await RENDER.stations();
  } catch (ex) {
    err.textContent = ex.detail || ex.message || 'Cannot delete: station may have images attached.';
    err.hidden = false;
  }
}

function _openImportModal(type) {
  const modal = $('#stationImportModal');
  if (!modal) return;
  $('#stationImportType').value = type;
  const isGeoJSON = type === 'geojson';
  $('#stationImportModalTitle').textContent = isGeoJSON ? 'Import Stations (GeoJSON)' : 'Import Stations (CSV)';
  $('#stationImportHelp').innerHTML = isGeoJSON
    ? 'Paste a GeoJSON <code>FeatureCollection</code> of <code>Point</code> features. Properties: <code>station_id, name, zone, village_dist_km</code>.'
    : 'Paste CSV with columns: <code>station_id, name, lat, lon, zone, village_dist_km, folder_hint</code>';
  $('#stationImportText').value    = '';
  $('#stationImportText').placeholder = isGeoJSON
    ? '{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[79.321,21.754]},"properties":{"station_id":"PN-C-025","name":"Bamboo Waterhole","zone":"core"}}]}'
    : 'station_id,name,lat,lon,zone\nPN-C-025,Bamboo Trail,21.754,79.321,core';
  $('#stationImportError').hidden   = true;
  $('#stationImportSuccess').hidden = true;
  modal.hidden = false;
}

async function _submitImportForm(e) {
  e?.preventDefault();
  const type   = $('#stationImportType').value;
  const text   = $('#stationImportText').value.trim();
  const errEl  = $('#stationImportError');
  const sucEl  = $('#stationImportSuccess');
  errEl.hidden = true;
  sucEl.hidden = true;

  if (!text) { errEl.textContent = 'Please paste the CSV or GeoJSON text.'; errEl.hidden = false; return; }

  const rid  = S.reserve?.reserve_id;
  const ep   = type === 'geojson' ? 'import/geojson' : 'import/csv';
  const body = type === 'geojson' ? { geojson: text } : { csv: text };
  try {
    const res = await api(`/api/reserves/${encodeURIComponent(rid)}/stations/${ep}`, { method: 'POST', body });
    const errs = res.errors?.length ? `<br>⚠ ${res.errors.length} row(s) skipped: ${res.errors.slice(0, 3).join('; ')}` : '';
    sucEl.innerHTML = `✓ Created <strong>${res.created}</strong>, updated <strong>${res.updated}</strong>.${errs}`;
    sucEl.hidden = false;
    await RENDER.stations();
  } catch (ex) {
    errEl.textContent = ex.detail || ex.message || 'Import failed.';
    errEl.hidden = false;
  }
}

let _stationListenersAttached = false;

RENDER.stations = async function stations() {
  if (!S.reserve) return;
  const rid = S.reserve.reserve_id;

  // Fetch live station data
  try {
    _stationsCache = await api(`/api/reserves/${encodeURIComponent(rid)}/stations`);
  } catch (e) {
    _stationsCache = [];
    console.error('Failed to load stations:', e);
  }

  // Update tally badge in nav
  const tallyEl = $('#tallyStations');
  if (tallyEl) tallyEl.textContent = _stationsCache.length ? ` ${_stationsCache.length}` : '';

  // Summary stat cards
  const statsEl = $('#stationsStats');
  if (statsEl) {
    const total   = _stationsCache.length;
    const active  = _stationsCache.filter(s => (s.status || 'active') === 'active').length;
    const offline = total - active;
    const imgTot  = _stationsCache.reduce((a, s) => a + (s.image_count || 0), 0);
    statsEl.innerHTML = [
      { label: 'Total Stations',    val: total,   note: 'registered in reserve' },
      { label: 'Active Cameras',    val: active,  note: 'currently deployed' },
      { label: 'Offline / Retired', val: offline, note: 'inactive / removed' },
      { label: 'Images Catalogued', val: nf(imgTot), note: 'across all stations' },
    ].map(c => `<div class="card pad">
        <div class="card-label">${esc(c.label)}</div>
        <div class="num big">${c.val}</div>
        <div class="note">${c.note}</div>
      </div>`).join('');
  }

  // Render table
  const tbl = $('#stationsTable');
  if (tbl) {
    const cols = ['Station ID', 'Name', 'Zone', 'Status', 'Coordinates', 'Hardware', 'Deployed', 'Images', 'Last Image'];
    tbl.innerHTML = `<thead><tr>${cols.map(c => `<th>${c}</th>`).join('')}</tr></thead><tbody></tbody>`;
    tbl.querySelector('tbody').innerHTML = _renderStationsTable(_stationsCache);
    $$('.station-row').forEach(tr => {
      tr.addEventListener('click', () => _openStationModal('edit', _stationsCache.find(s => s.station_id === tr.dataset.sid)));
    });
  }

  // Update geojson export href to include reserve_id
  const expBtn = $('#exportStationsGeojson');
  if (expBtn) expBtn.href = `/api/reserves/${encodeURIComponent(rid)}/stations/export/geojson`;

  // Wire event listeners only once
  if (!_stationListenersAttached) {
    _stationListenersAttached = true;

    // Search + filter
    $('#stationSearchInput')?.addEventListener('input', _stationsApplyFilter);
    $('#stationFilterZone')?.addEventListener('change', _stationsApplyFilter);
    $('#stationFilterStatus')?.addEventListener('change', _stationsApplyFilter);

    // Add station button
    $('#addStationBtn')?.addEventListener('click', () => _openStationModal('create'));

    // Boundaries button & modal
    $('#editBoundariesBtn')?.addEventListener('click', async () => {
      const modal = $('#reserveBoundariesModal');
      if (!modal) return;
      $('#boundariesError').hidden = true;
      $('#boundariesSuccess').hidden = true;
      try {
        const bData = await api(`/api/reserves/${encodeURIComponent(rid)}/boundaries`);
        const payload = {
          core_geojson: bData.core_geojson || null,
          buffer_geojson: bData.buffer_geojson || null,
          corridor_geojson: bData.corridor_geojson || null,
        };
        $('#boundariesGeojsonText').value = JSON.stringify(payload, null, 2);
      } catch {
        $('#boundariesGeojsonText').value = '';
      }
      modal.hidden = false;
    });

    $('#reserveBoundariesClose')?.addEventListener('click', () => { $('#reserveBoundariesModal').hidden = true; });
    $('#reserveBoundariesCancel')?.addEventListener('click', () => { $('#reserveBoundariesModal').hidden = true; });
    $('#reserveBoundariesModal')?.addEventListener('click', (e) => { if (e.target === $('#reserveBoundariesModal')) $('#reserveBoundariesModal').hidden = true; });
    $('#reserveBoundariesForm')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = $('#boundariesGeojsonText').value.trim();
      const errEl = $('#boundariesError');
      const sucEl = $('#boundariesSuccess');
      errEl.hidden = true;
      sucEl.hidden = true;
      if (!text) {
        errEl.textContent = 'Please provide GeoJSON text.';
        errEl.hidden = false;
        return;
      }
      try {
        let parsed = JSON.parse(text);
        if (parsed.type === 'Polygon' || parsed.type === 'MultiPolygon' || parsed.type === 'Feature' || parsed.type === 'FeatureCollection') {
          parsed = { core_geojson: parsed };
        }
        await api(`/api/reserves/${encodeURIComponent(rid)}/boundaries`, { method: 'PUT', body: parsed });
        sucEl.textContent = '✓ Reserve boundaries updated successfully.';
        sucEl.hidden = false;
        setTimeout(() => { $('#reserveBoundariesModal').hidden = true; }, 1200);
      } catch (err) {
        errEl.textContent = err.detail || err.message || 'Invalid JSON syntax for boundaries.';
        errEl.hidden = false;
      }
    });

    // Import buttons
    $('#importStationCsvBtn')?.addEventListener('click', () => _openImportModal('csv'));
    $('#importStationGeojsonBtn')?.addEventListener('click', () => _openImportModal('geojson'));

    // Station modal form
    $('#stationForm')?.addEventListener('submit', _submitStationForm);
    $('#stationModalClose')?.addEventListener('click', _closeStationModal);
    $('#stationFormCancel')?.addEventListener('click', _closeStationModal);
    $('#stationFormDelete')?.addEventListener('click', _deleteStation);

    // Import modal form
    $('#stationImportForm')?.addEventListener('submit', _submitImportForm);
    $('#stationImportModalClose')?.addEventListener('click', () => { $('#stationImportModal').hidden = true; });
    $('#stationImportCancel')?.addEventListener('click', () => { $('#stationImportModal').hidden = true; });

    // Close modals on overlay click
    $('#stationModal')?.addEventListener('click', e => { if (e.target === $('#stationModal')) _closeStationModal(); });
    $('#stationImportModal')?.addEventListener('click', e => { if (e.target === $('#stationImportModal')) $('#stationImportModal').hidden = true; });
  }
};

async function initApp() {

  if (!S.reserve) {
    const [reserves, cfg] = await Promise.all([api('/api/reserves'), api('/api/config')]);
    S.reserve = reserves[0];
    S.config = cfg;
  }
  if (!S.reserve) {
    document.querySelector('main').innerHTML =
      `<div class="card empty"><strong>No reserve set up yet</strong>
       Add a station list to begin. Run <span class="num">python -m tools.seed_demo</span>
       to load the demonstration reserve.</div>`;
    return;
  }
  if ($('#reserveName')) $('#reserveName').textContent = S.reserve.name;
  if ($('#authReserveName')) $('#authReserveName').textContent = `${S.reserve.name} · Offline Node`;
  try { await RENDER.run?.(); } catch (e) { console.error('Error in RENDER.run:', e); }
  try { await RENDER.review?.(); } catch (e) { console.error('Error in RENDER.review:', e); }
  try { guideInit(); } catch (e) { console.error('Error in guideInit:', e); }
  route();
}

/* Nothing above may end with an empty screen.
   initApp() is awaited from two places (boot, and the login form's submit
   handler). Both call hideAuth() FIRST, so if anything in initApp throws
   -- a failed /api/reserves, a missing element, a render bug -- the auth
   overlay is already gone, route() is never reached, no .view ever gets
   the `on` class, and the operator is left looking at a blank panel with
   no error and nothing to click. That is the single worst failure this UI
   can produce, and at a demo it is indistinguishable from the app being
   dead. This wrapper guarantees three things instead: the shell always
   routes, the failure is always visible, and there is always a way out. */
async function initAppSafely() {
  try {
    await initApp();
  } catch (err) {
    console.error('initApp failed:', err);
    try { route(); } catch (e) { console.error('route() also failed:', e); }
    const main = document.querySelector('main');
    if (main && !document.querySelector('.view.on')) {
      main.innerHTML = `
        <div class="card pad" style="max-width:640px;margin:var(--s6) auto">
          <h2 style="color:var(--act)">This screen could not finish loading</h2>
          <p class="note" style="margin-top:var(--s2)">The node is running and your
            sign-in worked, but one of the start-up steps failed, so the page was
            left empty. Nothing has been lost &mdash; this is a display failure,
            not a data failure.</p>
          <p class="note" style="margin-top:var(--s2)"><b>Details:</b>
            <span class="num">${esc(String(err && err.message || err))}</span></p>
          <div class="toolbar" style="margin-top:var(--s4)">
            <button type="button" class="primary" onclick="location.reload()">Reload the page</button>
            <button type="button" onclick="location.hash='#run';location.reload()">Go to Import Photos</button>
          </div>
        </div>`;
    }
  }
}

/* ── boot ──────────────────────────────────────────────────────────────── */
(async function boot() {
  /* Before auth, deliberately: the toggle has to work on the login screen
     too, and initApp() (where guideInit runs) is never reached until a
     reserve has loaded. */
  themeInit();
  navInit();
  authStageInit();
  setupAuth();
  window.addEventListener('hashchange', route);
  try {
    const r = await fetch('/api/auth/me');
    if (r.ok) {
      S.user = await r.json();
      updateUserUI();
      if (S.user.must_change_password) {
        showAuth('change');
      } else {
        hideAuth();
        await initAppSafely();
      }
    } else {
      showAuth('login');
    }
  } catch (e) {
    showAuth('login');
  }
})();
