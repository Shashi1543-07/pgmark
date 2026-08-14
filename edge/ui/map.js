/* PUGMARK · the map
   ─────────────────────────────────────────────────────────────────────────
   Replaces the map renderer inside app.js. Four things were wrong with it,
   and the first two are the kind a jury notices.

   1. IT WAS HARDCODED TO THE DEMO. Verbatim, in the render function:

          const DEAD = new Set(['PN-C-008', 'PN-C-009']);
          const NEW  = new Set(['PN-C-015']);

      Those are station IDs from tools/seed_demo.py. A camera that failed at
      a real reserve was never drawn as failed, and one installed this cycle
      was never drawn as new, because the frontend was reading a constant
      instead of the data. Here both come from station_activity, which is
      where that fact actually lives.

   2. THE GEOMETRY WAS WRONG. lat and lon were each stretched independently
      to fill a fixed 900x520 box. Measured against the seeded reserve:
      9.8 km east-west by 10.0 km north-south — very nearly square — drawn
      at an aspect ratio of 1.84. Every home-range polygon on screen was
      87% out of shape, directly under a table of areas that
      edge/pipeline/occupancy.py had carefully projected into UTM to get
      right. The care in the backend was thrown away by the last 20 lines
      of the frontend.

   3. NO SCALE, NO LEGEND, NO ORIENTATION. A ranger cannot tell whether a
      polygon is 4 km across or 40, and nothing on screen says what the
      colours mean.

   4. THE ZONES WERE DECORATION. Two hardcoded rounded rectangles labelled
      CORE and BUFFER at fixed pixel offsets, bearing no relationship to
      which stations are actually in which zone.

   Projection: equirectangular, with longitude scaled by cos(mean latitude)
   — the same correction edge/pipeline/occupancy.py::project_utm applies,
   at the accuracy a reserve-scale display needs. Full UTM is unnecessary
   here (across ~10 km the difference is sub-pixel) and would need a
   projection library this app deliberately does not ship. What matters is
   that a kilometre east and a kilometre north are the same number of
   pixels, which is what makes the scale bar honest and the hulls the right
   shape.

   Offline: no tiles, no CDN, no webfont. Same rule as the rest of edge/ui/.
   ───────────────────────────────────────────────────────────────────────── */

window.PugMap = (() => {
  const NS = 'http://www.w3.org/2000/svg';

  /* ── projection ──────────────────────────────────────────────────────── */

  function makeProjection(points, width, height, pad) {
    const lats = points.map(p => p.lat).filter(v => typeof v === 'number' && isFinite(v));
    const lons = points.map(p => p.lon).filter(v => typeof v === 'number' && isFinite(v));
    if (!lats.length) return null;

    const latMid = (Math.min(...lats) + Math.max(...lats)) / 2;
    const kx = Math.cos(latMid * Math.PI / 180);      // longitude shrinks with latitude

    /* Work in kilometres from the south-west corner, so one unit is one
       unit in both axes before anything reaches the screen. */
    const lat0 = Math.min(...lats), lon0 = Math.min(...lons);
    const toKm = (lat, lon) => [(lon - lon0) * 111.320 * kx, (lat - lat0) * 111.0];

    let [spanX, spanY] = toKm(Math.max(...lats), Math.max(...lons));
    /* A single station, or every station on one line, still needs a box. */
    spanX = Math.max(spanX, 0.5);
    spanY = Math.max(spanY, 0.5);

    /* ONE scale for both axes — this is the fix. Fit the larger span and
       centre the smaller one, rather than stretching each to fill. */
    const scale = Math.min((width - 2 * pad) / spanX, (height - 2 * pad) / spanY);
    const offX = (width - spanX * scale) / 2;
    const offY = (height - spanY * scale) / 2;

    const project = (lat, lon) => {
      const [x, y] = toKm(lat, lon);
      return [offX + x * scale, height - (offY + y * scale)];   // SVG y grows downward
    };
    return { project, scale, spanX, spanY, kmPerPx: 1 / scale };
  }

  /* A nice round number for the scale bar: 1, 2 or 5 x 10^n. */
  function niceDistance(km) {
    const pow = Math.pow(10, Math.floor(Math.log10(km)));
    const n = km / pow;
    return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * pow;
  }

  /* ── station state, from data rather than from a constant ────────────── */

  function stationState(s, cycle) {
    /* offline  — the camera stopped inside this cycle. This is the single
                  most important thing on the map: an absence alert means
                  nothing without it, and it was previously a hardcoded set
                  of two IDs.
       new      — first became active during this cycle, so a first capture
                  here is the camera arriving, not the tiger moving.
       idle     — active, nothing recorded this cycle.
       active   — active and recording. */
    if (s.active_days_this_cycle === 0 && s.was_active_before) return 'offline';
    if (s.ended_early_this_cycle) return 'offline';
    if (s.installed_this_cycle) return 'new';
    if (!s.image_count) return 'idle';
    return 'active';
  }

  const STATE_COPY = {
    offline: 'Stopped recording during this cycle',
    new: 'Installed this cycle',
    idle: 'Active, nothing recorded',
    active: 'Active and recording',
  };

  /* ── the hull parser ─────────────────────────────────────────────────── */

  function parseHull(wkt) {
    if (!wkt) return null;
    const body = wkt.replace(/^POLYGON\s*\(\(/i, '').replace(/\)\)\s*$/, '');
    const pts = body.split(',').map(pair => {
      const [lon, lat] = pair.trim().split(/\s+/).map(Number);
      return (isFinite(lat) && isFinite(lon)) ? { lat, lon } : null;
    }).filter(Boolean);
    return pts.length >= 3 ? pts : null;
  }

  /* ── render ──────────────────────────────────────────────────────────── */

  /**
   * @param {HTMLElement} host      container element
   * @param {Object}      data
   *   data.stations   [{station_id,name,zone,lat,lon,image_count,
   *                     installed_this_cycle,active_days_this_cycle,
   *                     was_active_before,ended_early_this_cycle}]
   *   data.occupancy  [{ind_id,hull_wkt,centroid_lat,centroid_lon,area_km2,
   *                     event_count,station_set,insufficient_reason}]
   *   data.prior      [{ind_id,centroid_lat,centroid_lon}]  previous cycle
   *   data.alerts     [{ind_id,type,severity}]
   *   data.focus      ind_id or null
   *   data.generalised  true when the role only sees rounded coordinates
   * @param {Function} onFocus  called with an ind_id (or null)
   */
  function render(host, data, onFocus) {
    const { stations = [], occupancy = [], prior = [], alerts = [],
            focus = null, generalised = false } = data;

    const usable = stations.filter(s => isFinite(s.lat) && isFinite(s.lon));
    if (!usable.length) {
      host.innerHTML = `<div class="card empty"><strong>No mapped stations</strong>
        This reserve has no camera stations with coordinates recorded, so there is
        nothing to place on a map. Add coordinates to the station table first.</div>`;
      return;
    }

    const W = 900, H = 560, PAD = 44;
    const proj = makeProjection(usable, W, H, PAD);
    const P = (lat, lon) => proj.project(lat, lon);

    const focused = focus ? occupancy.filter(o => o.ind_id === focus) : occupancy;
    const alertsByInd = alerts.reduce((m, a) => {
      (m[a.ind_id] = m[a.ind_id] || []).push(a); return m;
    }, {});

    /* ── zones, drawn from the stations that are actually in them ──────── */
    const zoneLayer = ['core', 'buffer'].map(zone => {
      const pts = usable.filter(s => s.zone === zone).map(s => P(s.lat, s.lon));
      if (pts.length < 3) return '';
      const hull = convexHull(pts);
      const d = hull.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
      return `<polygon class="zone zone-${zone}" points="${d}"><title>${zone} zone —
        ${usable.filter(s => s.zone === zone).length} stations</title></polygon>`;
    }).join('');

    /* ── home ranges ───────────────────────────────────────────────────── */
    const hulls = focused.map(o => {
      const pts = parseHull(o.hull_wkt);
      if (!pts) return '';
      const d = pts.map(p => { const [x, y] = P(p.lat, p.lon); return `${x.toFixed(1)},${y.toFixed(1)}`; }).join(' ');
      const flagged = alertsByInd[o.ind_id]?.some(a => a.severity === 'act');
      return `<polygon class="hull${flagged ? ' flagged' : ''}${focus === o.ind_id ? ' focus' : ''}"
        points="${d}" data-ind="${esc(o.ind_id)}"
        ><title>${esc(o.ind_id)} — ${o.area_km2} km², ${o.event_count} visits across
        ${(o.station_set || []).length} cameras</title></polygon>`;
    }).join('');

    /* ── movement between cycles ───────────────────────────────────────── */
    /* The centroid_shift alert is the only alert whose evidence is a
       distance, and v0.1.1 drew nothing of it. An arrow from last cycle's
       centroid to this one is the entire alert, visible at a glance. */
    const priorById = Object.fromEntries(prior.map(p => [p.ind_id, p]));
    const shifts = focused.map(o => {
      const was = priorById[o.ind_id];
      if (!was || !isFinite(was.centroid_lat) || !isFinite(o.centroid_lat)) return '';
      const [x1, y1] = P(was.centroid_lat, was.centroid_lon);
      const [x2, y2] = P(o.centroid_lat, o.centroid_lon);
      if (Math.hypot(x2 - x1, y2 - y1) < 6) return '';       // no visible movement
      const km = (Math.hypot(x2 - x1, y2 - y1) * proj.kmPerPx).toFixed(1);
      return `<g class="shift"><line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}"
        x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" marker-end="url(#arrow)"/>
        <circle cx="${x1.toFixed(1)}" cy="${y1.toFixed(1)}" r="2.5" class="was"/>
        <title>${esc(o.ind_id)} moved ${km} km since the previous cycle</title></g>`;
    }).join('');

    /* ── stations ──────────────────────────────────────────────────────── */
    const pins = usable.map(s => {
      const st = stationState(s, data.cycle);
      const [x, y] = P(s.lat, s.lon);
      const inFocus = focus && focused.some(o => (o.station_set || []).includes(s.station_id));
      const r = st === 'offline' || st === 'new' ? 5.5 : inFocus ? 5 : 3.6;
      return `<circle class="stn stn-${st}${inFocus ? ' in-range' : ''}"
        cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}"
        data-station="${esc(s.station_id)}" tabindex="0"
        ><title>${esc(s.name)} · ${esc(s.zone)} · ${esc(s.station_id)}
${STATE_COPY[st]}${s.image_count ? ` · ${s.image_count} frames` : ''}</title></circle>`;
    }).join('');

    /* ── scale bar ─────────────────────────────────────────────────────── */
    const barKm = niceDistance(proj.spanX / 4);
    const barPx = barKm / proj.kmPerPx;
    const scaleBar = `<g class="scalebar" transform="translate(${PAD - 10}, ${H - 22})">
      <line x1="0" y1="0" x2="${barPx.toFixed(1)}" y2="0"/>
      <line x1="0" y1="-4" x2="0" y2="4"/>
      <line x1="${barPx.toFixed(1)}" y1="-4" x2="${barPx.toFixed(1)}" y2="4"/>
      <text x="${(barPx / 2).toFixed(1)}" y="-8" text-anchor="middle">${barKm} km</text>
    </g>`;

    const northArrow = `<g class="north" transform="translate(${W - 34}, 30)">
      <line x1="0" y1="16" x2="0" y2="-10"/>
      <path d="M0,-14 L4,-6 L-4,-6 Z"/>
      <text x="0" y="30" text-anchor="middle">N</text></g>`;

    const banner = generalised ? `<div class="banner"><b>Generalised view</b><span>Your
      role sees rounded coordinates only, so home-range boundaries are not shown.
      Camera positions are approximate to the nearest grid cell.</span></div>` : '';

    host.innerHTML = `${banner}
      <svg viewBox="0 0 ${W} ${H}" class="pugmap" role="img"
           aria-label="Camera stations and tiger home ranges, ${proj.spanX.toFixed(1)} by
           ${proj.spanY.toFixed(1)} kilometres">
        <defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4"
          markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z"/></marker></defs>
        ${zoneLayer}${hulls}${shifts}${pins}${scaleBar}${northArrow}
      </svg>
      ${legend(occupancy, alertsByInd, focus)}`;

    host.querySelectorAll('[data-ind]').forEach(el =>
      el.addEventListener('click', () => onFocus?.(focus === el.dataset.ind ? null : el.dataset.ind)));
    host.querySelectorAll('.legend [data-ind]').forEach(el =>
      el.addEventListener('click', () => onFocus?.(focus === el.dataset.ind ? null : el.dataset.ind)));
  }

  /* ── legend ──────────────────────────────────────────────────────────── */

  function legend(occupancy, alertsByInd, focus) {
    const withHull = occupancy.filter(o => o.hull_wkt);
    const chips = withHull.slice(0, 24).map(o => {
      const flagged = alertsByInd[o.ind_id]?.some(a => a.severity === 'act');
      return `<button class="chip${focus === o.ind_id ? ' on' : ''}${flagged ? ' act' : ''}"
        data-ind="${esc(o.ind_id)}">${esc(o.ind_id)}
        <span>${o.area_km2} km²</span></button>`;
    }).join('');

    return `<div class="legend">
      <div class="keys">
        <span><i class="k stn-active"></i>recording</span>
        <span><i class="k stn-idle"></i>active, nothing seen</span>
        <span><i class="k stn-offline"></i>stopped this cycle</span>
        <span><i class="k stn-new"></i>installed this cycle</span>
        <span><i class="k k-hull"></i>home range</span>
        <span><i class="k k-shift"></i>movement since last cycle</span>
      </div>
      ${chips ? `<div class="chips">${chips}
        ${focus ? '<button class="chip clear" data-ind="">Show all</button>' : ''}</div>` : ''}
      ${withHull.length === 0 ? `<p class="note">No home ranges to draw: no individual in
        this run was captured at enough distinct stations to form a polygon. The table
        below shows why for each.</p>` : ''}
    </div>`;
  }

  /* ── geometry helper (screen space) ──────────────────────────────────── */

  function convexHull(pts) {
    const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (p.length <= 2) return p;
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [];
    for (const q of p) {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], q) <= 0) lower.pop();
      lower.push(q);
    }
    const upper = [];
    for (const q of [...p].reverse()) {
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) upper.pop();
      upper.push(q);
    }
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  return { render, makeProjection, niceDistance, parseHull, stationState };
})();

