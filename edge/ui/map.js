/* PUGMARK · Interactive Offline GIS Map & Intelligence Visualizer
   ─────────────────────────────────────────────────────────────────────────
   Real slippy map, entirely offline. Leaflet is vendored into
   /ui/vendor/leaflet/ and the satellite imagery is a tile pyramid on this
   machine's own disk at /ui/tiles/{z}/{x}/{y}.jpg, built once by
   `python -m tools.fetch_basemap_tiles`. Nothing here touches the network,
   which is the whole point: the deployment target is a range-office laptop
   with no internet, and a grey map at the demo is a lost hackathon
   (CLAUDE.md rule 3, BLUEPRINT.md sec 8 "the offline map trap").

   WHY THIS REPLACED A HAND-ROLLED SVG MAP
   The previous version projected lat/lon into a fixed 1000x640 viewBox with
   its own equirectangular maths, and re-implemented panning, wheel zoom, a
   scalebar and a north arrow on top of it. All of that worked, but it was
   several hundred lines of map plumbing that a mapping library does better,
   and it had two failures no amount of patching fixes: the imagery was ONE
   raster stretched to the viewBox, so zooming in turned the reserve into a
   blur, and every redraw threw away the SVG and rebuilt it, so pan/zoom
   position was lost whenever anything refreshed.

   Leaflet gives real tile pyramids, inertial pan, pinch zoom, a correct
   scale bar, and -- because the map instance now OUTLIVES a render -- the
   view survives a refresh. What is deliberately kept from the old file:
   every piece of domain logic (station states, hull parsing, tiger colours,
   the alert-filtered movement layer, both drawers, the playback scrubber).
   Those were right; only the rendering substrate was wrong.
   ───────────────────────────────────────────────────────────────────────── */

window.PugMap = (() => {

  const TIGER_PALETTE = [
    { fill: '#d97706', stroke: '#b45309' }, // Tiger Amber
    { fill: '#059669', stroke: '#047857' }, // Emerald
    { fill: '#2563eb', stroke: '#1d4ed8' }, // Sapphire
    { fill: '#dc2626', stroke: '#b91c1c' }, // Ruby Red
    { fill: '#7c3aed', stroke: '#6d28d9' }, // Royal Violet
    { fill: '#0891b2', stroke: '#0e7490' }, // Teal Cyan
    { fill: '#ea580c', stroke: '#c2410c' }, // Tangerine
    { fill: '#65a30d', stroke: '#4d7c0f' }, // Forest Lime
    { fill: '#e11d48', stroke: '#be123c' }, // Rose Crimson
    { fill: '#4f46e5', stroke: '#4338ca' }, // Deep Indigo
    { fill: '#0d9488', stroke: '#0f766e' }, // Sea Green
    { fill: '#9333ea', stroke: '#7e22ce' }, // Amethyst
    { fill: '#ca8a04', stroke: '#a16207' }, // Gold
    { fill: '#0284c7', stroke: '#0369a1' }, // Sky Blue
    { fill: '#be185d', stroke: '#9d174d' }, // Magenta
    { fill: '#475569', stroke: '#334155' }, // Slate
  ];

  function getTigerColor(ind_id) {
    if (!ind_id) return TIGER_PALETTE[0];
    let hash = 0;
    for (let i = 0; i < ind_id.length; i++) {
      hash = (hash * 31 + ind_id.charCodeAt(i)) & 0xffffffff;
    }
    return TIGER_PALETTE[Math.abs(hash) % TIGER_PALETTE.length];
  }

  /* ── helpers kept verbatim from the SVG implementation ────────────────
     These describe the DOMAIN, not the drawing surface, so swapping the
     renderer must not disturb them. makeProjection is no longer used to
     draw anything -- Leaflet projects now -- but it stays exported because
     it is part of this module's published surface. */

  function makeProjection(points, width, height, pad) {
    if (!points || !points.length) return null;
    const lats = points.map(p => Number(p.lat)).filter(v => typeof v === 'number' && isFinite(v));
    const lons = points.map(p => Number(p.lon)).filter(v => typeof v === 'number' && isFinite(v));
    if (!lats.length || !lons.length) return null;

    const latMin = Math.min(...lats), latMax = Math.max(...lats);
    const lonMin = Math.min(...lons), lonMax = Math.max(...lons);

    const latMid = (latMin + latMax) / 2;
    const kx = Math.cos(latMid * Math.PI / 180) || 1.0;

    const lat0 = latMin, lon0 = lonMin;
    const toKm = (lat, lon) => [(lon - lon0) * 111.320 * kx, (lat - lat0) * 111.0];

    let [spanX, spanY] = toKm(latMax, lonMax);
    spanX = Math.max(spanX, 1.0);
    spanY = Math.max(spanY, 1.0);

    const scale = Math.min((width - 2 * pad) / spanX, (height - 2 * pad) / spanY) || 10;
    const offX = (width - spanX * scale) / 2;
    const offY = (height - spanY * scale) / 2;

    const project = (lat, lon) => {
      const [x, y] = toKm(Number(lat), Number(lon));
      return [offX + x * scale, height - (offY + y * scale)];
    };
    return { project, scale, spanX, spanY, kmPerPx: 1 / scale, lat0, lon0, kx };
  }

  function niceDistance(km) {
    if (!km || !isFinite(km) || km <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(km)));
    const n = km / pow;
    return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * pow;
  }

  function stationState(s) {
    if (!s) return 'idle';
    if (s.active_days_this_cycle === 0 && s.was_active_before) return 'offline';
    if (s.ended_early_this_cycle) return 'offline';
    if (s.installed_this_cycle) return 'new';
    if (!s.image_count) return 'idle';
    return 'active';
  }

  const STATE_COPY = {
    offline: '🔴 Camera stopped recording',
    new: '🟡 Installed this cycle',
    idle: '⚪ Working, 0 tiger frames',
    active: '🟢 Active & recording',
  };

  function parseHull(wkt) {
    if (!wkt || typeof wkt !== 'string') return null;
    const body = wkt.replace(/^POLYGON\s*\(\(/i, '').replace(/\)\)\s*$/, '');
    const pts = body.split(',').map(pair => {
      const parts = pair.trim().split(/\s+/).map(Number);
      if (parts.length >= 2 && isFinite(parts[0]) && isFinite(parts[1])) {
        return { lon: parts[0], lat: parts[1] };
      }
      return null;
    }).filter(Boolean);
    return pts.length >= 3 ? pts : null;
  }

  function convexHull(pts) {
    if (!pts || pts.length <= 2) return pts || [];
    const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
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

  /* ── coordinates, strictly ─────────────────────────────────────────────
     `isFinite(null)` is TRUE in JavaScript, because Number(null) is 0. Every
     coordinate guard in the previous map used isFinite() directly, so an
     individual with a null centroid -- an ordinary, expected state, it means
     too few captures to place one -- passed validation and was drawn at
     0N 0E. The SVG renderer swallowed that as a NaN path and showed nothing;
     Leaflet's L.latLng returns null for it and throws on the next call,
     which is how a long-standing bug finally became visible.

     One helper, used for every lat/lon that enters this file. */
  const num = (v) => {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const latlng = (lat, lon) => {
    const a = num(lat), b = num(lon);
    return (a === null || b === null) ? null : window.L.latLng(a, b);
  };

  /* ── the world, as geometry ────────────────────────────────────────────
     Natural Earth coastlines, country borders, state/province borders and
     lakes, fetched once by tools/fetch_basemap_vectors.py and served off
     this machine's disk. This is the layer UNDER the satellite imagery, and
     it is what makes the map a map rather than a photograph of one region:
     the satellite pyramid only covers Pench, so before this existed zooming
     out ran off the edge of the data and showed nothing at all.

     Geometry rather than pixels is also what lets the whole world change
     colour with the theme -- a raster basemap would be a fixed picture. */
  let worldData = null;
  let worldPromise = null;

  function loadWorld() {
    if (worldPromise) return worldPromise;
    const want = ['ocean', 'countries', 'states', 'lakes'];
    worldPromise = Promise.all(want.map(n =>
      fetch(`/ui/geo/${n}.geojson`).then(r => (r.ok ? r.json() : null)).catch(() => null)
    )).then(parts => {
      worldData = Object.fromEntries(want.map((n, i) => [n, parts[i]]));
      return worldData;
    });
    return worldPromise;
  }

  /* Dark mode is a green-black relief map; light mode is the plain white
     one people expect from a printed map. Both are decided here rather than
     in CSS because these shapes are drawn to a CANVAS -- 950 polygons as
     individual SVG paths is a real cost on the GPU-less laptop this has to
     run on, and a canvas cannot be styled by stylesheet. */
  function worldPalette() {
    /* This MUST mirror app.js's own definition exactly, which is:
           dark  -> <html data-theme="dark">
           light -> the attribute is REMOVED
       There is no third "follow the system" state anywhere in this app, and
       app.css has no prefers-color-scheme rule at all. An earlier version of
       this function treated a missing attribute as "ask the operating
       system", so on a machine whose OS is in dark mode the page went light
       while the map stayed green -- the theme button appeared to do nothing
       to the map. Asking the OS was the bug; the attribute is the whole
       truth. */
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    return dark ? {
      key: 'dark',
      ocean:        '#04120b',
      land:         '#0f2a1b',
      landStroke:   '#356f4e',
      stateStroke:  '#1f4a33',
      lake:         '#062b1e',
      lakeStroke:   '#1d5f45',
    } : {
      key: 'light',
      ocean:        '#e8f0ec',
      land:         '#ffffff',
      landStroke:   '#aebcb4',
      stateStroke:  '#d7e0da',
      lake:         '#dbe8f1',
      lakeStroke:   '#bcd2e2',
    };
  }

  /* ── state ────────────────────────────────────────────────────────────
     `map` and `groups` outlive a render on purpose: rebuilding the map on
     every refresh is exactly what used to throw the user's pan and zoom
     away. A render swaps the CONTENTS of the layer groups and leaves the
     view where the user put it. */
  let map = null;
  let mapHost = null;
  let tiles = null;
  let groups = null;
  let homeBounds = null;
  let lastFocus = undefined;
  let lastRender = null;
  let basemapMeta = null;
  let lastLayerClick = 0;   // see the map 'click' handler in buildMap()
  let worldPaletteKey = null;   // rebuild the world only when the theme flips
  let worldRenderer = null;     // one shared canvas for ~950 polygons
  let imageryBounds = null;     // where satellite tiles actually exist

  const viewState = {
    playbackTimer: null,
    playbackIndex: 0,
    layers: {
      /* Movement arrows and territory shading are the two layers that turn
         the reserve view into a tangle of crossing lines when more than a
         handful of individuals are on screen. They are genuinely useful one
         tiger at a time, so they stay available -- just not all at once by
         default. Clicking a single tiger still draws its own range and its
         own movement regardless of these. The movement layer's own data is
         further filtered to alert-bearing individuals when nothing is
         focused (see the movement section below) -- these two guards are
         complementary, not redundant: this one keeps a fresh reserve view
         uncluttered by default, that one keeps it uncluttered even after
         someone switches the layer on. */
      basemap: true,
      boundaries: true,
      ranges: false,
      stations: true,
      zones: true,
      movement: false,
      corridors: true,
    },
  };

  /* ── the basemap manifest ─────────────────────────────────────────────
     Written by tools/fetch_basemap_tiles.py alongside the pyramid it
     fetched, so the zoom range and bounding box the UI configures Leaflet
     with are the ones that were actually downloaded, rather than numbers
     copied into JavaScript and left to drift. */
  window.PUGMARK_BASEMAP = null;
  const basemapReady = fetch('/ui/img/basemap-pench.json')
    .then(r => (r.ok ? r.json() : null))
    .then(meta => {
      if (!meta) return null;
      basemapMeta = meta;
      window.PUGMARK_BASEMAP = meta;
      return meta;
    })
    .catch(() => null);   // no manifest on disk: the vector map still works

  /* ── the detail drawer ──────────────────────────────────────────────────
     It used to be closable ONLY by its own small x, which is the wrong
     default for a panel that covers the thing you are looking at: the
     natural gesture is to click the map again, or press Escape. Both work
     now, and opening a different pin replaces the contents rather than
     stacking. */
  function closeMapDrawer() {
    document.getElementById('mapSideDrawer')?.classList.remove('open');
  }

  (function wireDrawerDismissal() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('[data-close-drawer]')) { closeMapDrawer(); return; }
      const drawer = document.getElementById('mapSideDrawer');
      if (!drawer || !drawer.classList.contains('open')) return;
      if (e.target.closest('#mapSideDrawer')) return;
      // a click on a pin, hull or control is not a dismissal -- those either
      // open a drawer of their own or belong to the map furniture
      if (e.target.closest('.stn, .hull, .roster-row, .map-layer-btn, .map-zoom-btn')) return;
      closeMapDrawer();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeMapDrawer();
    });
  })();

  function refresh() {
    if (!lastRender) return;
    render(lastRender.host, lastRender.data, lastRender.onFocus);
  }

  /* ── map construction, once ─────────────────────────────────────────── */

  function buildMap(host) {
    const L = window.L;
    const meta = basemapMeta || {};
    const pad = Number(meta.pad_deg) || 0.06;

    // The satellite tiles cover only this box. It still constrains the TILE
    // LAYER -- there is no imagery outside it -- but it no longer constrains
    // the MAP, which now has world vector geometry underneath and so has
    // somewhere to go when you zoom out.
    const bounds = (isFinite(meta.north) && isFinite(meta.south))
      ? L.latLngBounds([meta.south - pad, meta.west - pad], [meta.north + pad, meta.east + pad])
      : null;

    map = L.map(host, {
      // the app has its own styled zoom buttons in the corner already
      zoomControl: false,
      attributionControl: true,
      // z2 puts the whole planet in the panel; z17 is a single clearing.
      // The old floor of z10 was the reason zooming out stopped at the
      // reserve boundary -- there was nothing below it to show.
      minZoom: 2,
      maxZoom: 17,
      maxBounds: L.latLngBounds([-85, -185], [85, 185]),
      maxBoundsViscosity: 0.6,
      zoomSnap: 0.5,
      wheelPxPerZoomLevel: 90,
      worldCopyJump: false,
    });
    // keeps every `.pugmap .stn-*` themed rule in app.css matching, so the
    // dark/light map palette survived this rewrite untouched
    host.classList.add('pugmap');

    // A view MUST be set before any layer is added: Leaflet throws
    // "Set map center and zoom first" otherwise, and because app.js wraps
    // the render in a try/catch that failure surfaced as a blank map rather
    // than as an error. render() refines this to the station extent as soon
    // as it knows it; this is only the floor.
    if (bounds) map.fitBounds(bounds);
    else map.setView([21.64, 79.32], 11);
    worldRenderer = L.canvas({ pane: 'pug-world', padding: 0.3 });

    tiles = L.tileLayer(meta.tiles || '/ui/tiles/{z}/{x}/{y}.jpg', {
      // below this the imagery does not exist; the vector world shows instead
      minZoom: Number(meta.min_zoom) || 10,
      // z14 is the deepest level on disk. Leaflet upscales cached z14 tiles
      // past that instead of requesting levels that were never downloaded,
      // which is what keeps a close-in look at one camera sharp enough
      // without shipping the 51 MB that a real z15 would have cost.
      maxNativeZoom: Number(meta.max_native_zoom) || 14,
      maxZoom: 17,
      bounds,
      noWrap: true,
      keepBuffer: 2,
      attribution: esc(meta.attribution || 'Imagery © Esri, Maxar, Earthstar Geographics'),
      className: 'pug-tiles',
    });

    /* Stacking order must come from MEANING, not from the order somebody
       happened to click the layer buttons in. Leaflet paints layer groups in
       the order they are added to the map, so switching Home Ranges on after
       the map had loaded dropped the territory polygons on top of the camera
       pins -- and a polygon is interactive, so it ate every click meant for a
       station. The pins became unclickable purely because of toggle order.

       Named panes fix the order once. A camera is the most specific thing on
       the map and always takes the click; ground shading is the least and
       never does. */
    // 'world' is deliberately below Leaflet's own tilePane (z-index 200):
    // the vector planet is the floor, satellite imagery paints on top of it
    // wherever imagery exists, and everything else stacks above both.
    const PANES = { world: 150, ground: 410, ranges: 420, movement: 430,
                    stations: 440, playback: 450, focus: 460 };
    Object.entries(PANES).forEach(([name, z]) => {
      map.createPane('pug-' + name);
      map.getPane('pug-' + name).style.zIndex = String(z);
    });

    groups = {
      world: L.layerGroup(),
      boundaries: L.layerGroup(),
      zones: L.layerGroup(),
      ranges: L.layerGroup(),
      movement: L.layerGroup(),
      stations: L.layerGroup(),
      playback: L.layerGroup(),
      focus: L.layerGroup(),
    };
    if (viewState.layers.basemap) tiles.addTo(map);
    Object.entries(groups).forEach(([name, g]) => {
      // playback, world and focus are not user layers: focus in particular
      // must never depend on a toggle, because it exists to answer "show me
      // THIS tiger" -- see the focus section in render()
      if (name === 'playback' || name === 'world' || name === 'focus'
          || viewState.layers[name] !== false) g.addTo(map);
    });

    L.control.scale({ position: 'bottomleft', metric: true, imperial: false }).addTo(map);
    map.attributionControl.addAttribution('Boundaries: Natural Earth');

    /* Reserve-scale furniture is meaningless at country scale. "CORE
       SANCTUARY", "BUFFER SECTOR" and "WILDLIFE CORRIDOR" describe features a
       few kilometres across; zoomed out to India they collapse onto the same
       pixel and render as one illegible pile of text on top of Pench. They
       are labels for a scale, so they appear at that scale. */
    const syncScale = () => host.classList.toggle('at-reserve-scale', map.getZoom() >= 10);
    map.on('zoomend', syncScale);
    syncScale();

    /* Satellite imagery exists for the reserve and nowhere else -- it is
       12.8 MB of tiles carried on a laptop with no internet, not a live tile
       service, and covering the planet at this depth would be hundreds of
       gigabytes. That limit is real and permanent, so the interface has to
       STATE it rather than let the button sit lit while nothing appears,
       which just reads as broken (CLAUDE.md rule 8: a refusal is a valid
       output, provided it explains itself). */
    imageryBounds = bounds;
    map.on('zoomend moveend', syncImagery);

    /* Clicking bare ground dismisses the drawer -- but ONLY bare ground.
       Leaflet propagates a layer's click up to the map as well, so a click
       on a camera pin fires the pin's handler (which opens the drawer) and
       then this one (which closed it again), and the drawer appeared to do
       nothing at all. L.DomEvent.stopPropagation does not help here: it
       marks the DOM event, and Evented.fire propagates to the map without
       consulting that mark. A short guard set by the layer handlers is what
       actually distinguishes "clicked the map" from "clicked a thing on the
       map". */
    map.on('click', () => {
      if (Date.now() - lastLayerClick < 300) return;
      closeMapDrawer();
    });
    return map;
  }

  /* ── drawing the world ────────────────────────────────────────────────
     Rebuilt only when the theme flips, never on an ordinary render. A
     render happens on every data change and focus change; re-adding ~950
     polygons each time would make clicking a tiger feel like the app had
     hung, which is precisely the sort of cost a laptop with no GPU cannot
     absorb. */
  function drawWorld() {
    if (!map || !worldData) return;
    const pal = worldPalette();
    if (worldPaletteKey === pal.key && groups.world.getLayers().length) return;
    worldPaletteKey = pal.key;
    groups.world.clearLayers();

    const add = (data, style) => {
      if (!data) return;
      L.geoJSON(data, {
        pane: 'pug-world',
        renderer: worldRenderer,
        interactive: false,     // the world is scenery; clicks belong to the reserve
        style: () => style,
      }).addTo(groups.world);
    };

    // ocean first so land sits on it, then borders, then inland water
    add(worldData.ocean, { color: pal.ocean, weight: 0, fillColor: pal.ocean, fillOpacity: 1 });
    add(worldData.countries, { color: pal.landStroke, weight: 0.9,
                               fillColor: pal.land, fillOpacity: 1 });
    add(worldData.states, { color: pal.stateStroke, weight: 0.55,
                            fillColor: pal.land, fillOpacity: 0 });
    add(worldData.lakes, { color: pal.lakeStroke, weight: 0.5,
                           fillColor: pal.lake, fillOpacity: 1 });

    // the panel behind the geometry has to match, or the ocean stops at the
    // edge of the drawn world and the difference reads as a rendering fault
    if (mapHost) mapHost.style.background = pal.ocean;
  }

  /* Is satellite imagery available for what is currently on screen? */
  function imageryAvailable() {
    if (!map || !imageryBounds) return false;
    return map.getZoom() >= 10 && imageryBounds.intersects(map.getBounds());
  }

  function syncImagery() {
    const btn = document.querySelector('.map-layer-btn[data-layer="basemap"]');
    const note = document.getElementById('mapImageryNote');
    const ok = imageryAvailable();
    if (btn) {
      btn.classList.toggle('unavailable', !ok);
      btn.title = ok
        ? 'Satellite imagery for Pench Tiger Reserve'
        : 'Satellite imagery covers the reserve only — it is carried on this '
          + 'machine, not streamed. Zoom in on Pench to see it.';
    }
    if (note) {
      note.textContent = 'Satellite: reserve only (offline imagery)';
      note.classList.toggle('show', viewState.layers.basemap && !ok);
    }
  }

  function setLayerVisible(name, on) {
    if (!map) return;
    if (name === 'basemap') {
      if (on) { if (!map.hasLayer(tiles)) tiles.addTo(map); }
      else if (map.hasLayer(tiles)) map.removeLayer(tiles);
      return;
    }
    const g = groups?.[name];
    if (!g) return;
    if (on) { if (!map.hasLayer(g)) g.addTo(map); }
    else if (map.hasLayer(g)) map.removeLayer(g);
  }

  /* ── render ───────────────────────────────────────────────────────────
     Called on every data change, focus change and theme change. It must be
     cheap and it must not move the view. */

  function render(host, data, onFocus) {
    lastRender = { host, data, onFocus };
    if (!host) return;

    if (!window.L) {
      // Leaflet failed to load. Say so rather than leaving a blank rectangle
      // that looks like missing data (CLAUDE.md rule 8).
      host.innerHTML = `<div class="card empty" style="padding:var(--s4);text-align:center">
        <strong>Map engine unavailable</strong><br>
        /ui/vendor/leaflet/leaflet.js did not load, so the map cannot draw.
        Every other screen is unaffected.</div>`;
      return;
    }
    const L = window.L;

    const rawStations = data?.stations || [];
    const occupancy = Array.isArray(data?.occupancy) ? data.occupancy : [];
    const prior = Array.isArray(data?.prior) ? data.prior : [];
    const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
    const boundaries = data?.boundaries || {};
    const events = Array.isArray(data?.events) ? data.events : [];
    const focus = data?.focus || null;

    const usable = rawStations.filter(s => s && num(s.lat) !== null && num(s.lon) !== null);
    if (!usable.length) {
      if (map) { map.remove(); map = null; mapHost = null; }
      host.innerHTML = `<div class="card empty" style="padding:var(--s4);text-align:center">
        <strong>No mapped stations</strong><br>
        This reserve has no camera stations with coordinates recorded.</div>`;
      return;
    }

    if (map && mapHost !== host) { map.remove(); map = null; }
    if (!map) {
      host.innerHTML = '';
      mapHost = host;
      buildMap(host);
      homeBounds = null;
    }

    // Leaflet measures its container on creation, and the map view is a tab
    // that is display:none until it is opened -- so the first build often
    // measures zero. Re-measuring after layout is what stops the map from
    // rendering as a single tile in the corner.
    requestAnimationFrame(() => { try { map.invalidateSize({ animate: false }); } catch (e) {} });

    // the world is scenery and owns its own lifecycle, so it is not cleared
    // with the data layers -- it is rebuilt only when the theme changes
    Object.entries(groups).forEach(([name, g]) => { if (name !== 'world') g.clearLayers(); });
    if (worldData) drawWorld();
    else loadWorld().then(() => { drawWorld(); });

    const focused = focus ? occupancy.filter(o => o.ind_id === focus) : occupancy;
    const alertsByInd = alerts.reduce((m, a) => {
      if (a && a.ind_id) (m[a.ind_id] = m[a.ind_id] || []).push(a);
      return m;
    }, {});

    const tigersByStation = {};
    occupancy.forEach(o => {
      (o.station_set || []).forEach(stnId => {
        tigersByStation[stnId] = tigersByStation[stnId] || [];
        tigersByStation[stnId].push(o.ind_id);
      });
    });

    const tip = (html) => ({ sticky: true, className: 'pug-tip', direction: 'top', opacity: 1, html });

    /* ── 1. boundaries: core, buffer, corridor ───────────────────────── */

    function addGeoJson(geom, opts, label) {
      if (!geom) return;
      const layer = L.geoJSON(geom, {
        pane: 'pug-ground',
        style: () => opts,
        // GeoJSON is lon,lat and Leaflet handles that itself -- doing the
        // swap by hand here is the classic way to put a reserve in the
        // Arabian Sea.
      });
      layer.addTo(groups.boundaries);
      if (label) {
        /* Anchored to the north edge of THIS geometry, not to its centre.
           Core sits inside buffer, so all three boundaries share almost the
           same centre point -- three permanent centre labels landed on top of
           one another and rendered as an illegible smudge in the middle of
           the reserve. Their north edges are naturally separated (a contained
           polygon's edge is always inside its container's), so this spaces
           them without any hand-tuned offsets. */
        try {
          const bb = layer.getBounds();
          if (bb.isValid()) {
            L.marker([bb.getNorth(), bb.getCenter().lng], {
              pane: 'pug-ground',
              interactive: false,
              icon: L.divIcon({ className: 'boundary-label', html: `<span>${label}</span>`,
                                iconSize: [200, 14], iconAnchor: [100, -4] }),
            }).addTo(groups.boundaries);
          }
        } catch (e) { /* a label is decoration; never let it take the map down */ }
      }
    }

    let hasExplicitGeoJson = false;
    if (boundaries.core_geojson) {
      addGeoJson(boundaries.core_geojson,
        { color: '#10b981', weight: 2, dashArray: '6 4', fillColor: '#059669', fillOpacity: 0.10, className: 'boundary-core' },
        'CORE SANCTUARY');
      hasExplicitGeoJson = true;
    }
    if (boundaries.buffer_geojson) {
      addGeoJson(boundaries.buffer_geojson,
        { color: '#f59e0b', weight: 2, dashArray: '8 5', fillColor: '#d97706', fillOpacity: 0.06, className: 'boundary-buffer' },
        'BUFFER SECTOR');
      hasExplicitGeoJson = true;
    }
    if (boundaries.corridor_geojson && viewState.layers.corridors) {
      addGeoJson(boundaries.corridor_geojson,
        { color: '#22d3ee', weight: 3, dashArray: '10 6', fillOpacity: 0, className: 'boundary-corridor' },
        '🛤 WILDLIFE CORRIDOR');
      hasExplicitGeoJson = true;
    }

    // Fallback demarcation when the reserve has no boundary GeoJSON on file:
    // the convex hull of its own stations is an honest approximation and is
    // labelled as derived, not presented as a surveyed border.
    if (!hasExplicitGeoJson && usable.length >= 3) {
      const hull = convexHull(usable.map(s => [Number(s.lon), Number(s.lat)]));
      if (hull.length >= 3) {
        L.polygon(hull.map(([lon, lat]) => [lat, lon]), {
          pane: 'pug-ground',
          color: '#10b981', weight: 2, dashArray: '8 4', fillOpacity: 0.04,
          className: 'derived-sanctuary-boundary',
        }).bindTooltip('SANCTUARY PERIMETER (derived from station extent)',
                       { className: 'pug-tip', sticky: true })
          .addTo(groups.boundaries);
      }
    }

    /* ── 2. derived core / buffer zone shading ───────────────────────── */
    ['buffer', 'core'].forEach(zone => {
      const pts = usable.filter(s => (s.zone || '').toLowerCase() === zone)
                        .map(s => [Number(s.lon), Number(s.lat)]);
      if (pts.length < 3) return;
      const hull = convexHull(pts);
      if (hull.length < 3) return;
      const count = pts.length;
      L.polygon(hull.map(([lon, lat]) => [lat, lon]), {
        pane: 'pug-ground',
        className: `zone zone-${zone}`,
        color: zone === 'core' ? '#34d399' : '#fbbf24',
        weight: 1.2, dashArray: '4 4',
        fillColor: zone === 'core' ? '#059669' : '#d97706',
        fillOpacity: 0.07,
        interactive: false,
      }).bindTooltip(`${zone.toUpperCase()} zone · ${count} cameras`, tip())
        .addTo(groups.zones);
    });

    /* ── 3. home ranges and centroids ────────────────────────────────── */
    occupancy.forEach(o => {
      const isFocused = focus === o.ind_id;
      const isDimmed = focus && !isFocused;
      const pts = parseHull(o.hull_wkt);
      const color = getTigerColor(o.ind_id);
      const flagged = alertsByInd[o.ind_id]?.some(a => a.severity === 'act');
      const centroid = latlng(o.centroid_lat, o.centroid_lon);
      const hasCentroid = centroid !== null;

      const hullTip = () => {
        const alertsList = alertsByInd[o.ind_id] || [];
        return `<h4 style="color:${color.fill}">🐅 ${esc(o.ind_id)}</h4>
          <div class="meta-line"><b>Territory:</b> ${o.area_km2 || '—'} km²</div>
          <div class="meta-line"><b>Sightings:</b> ${o.event_count || 0} visits across ${(o.station_set || []).length} cameras</div>
          <div class="meta-line"><b>Camera effort:</b> ${o.effort_days || '—'} camera-days</div>
          ${alertsList.length ? `<div style="color:#ef4444;margin-top:4px">⚠ ${alertsList.length} active alert(s)</div>` : ''}
          <div class="tip-hint">Click to open the tiger drawer</div>`;
      };

      let shape = null;
      if (pts && pts.length >= 3) {
        shape = L.polygon(pts.map(p => [p.lat, p.lon]), {
          pane: 'pug-ranges',
          className: `hull ${isFocused ? 'focus' : ''} ${flagged ? 'flagged' : ''}`,
          color: isDimmed ? '#8aa398' : color.stroke,
          weight: isFocused ? 3 : isDimmed ? 1 : 1.8,
          fillColor: color.fill,
          fillOpacity: isFocused ? 0.40 : isDimmed ? 0.05 : 0.20,
        });
      } else if (hasCentroid) {
        // one or two stations is not a polygon. A soft radius says "somewhere
        // around here" without inventing a territory shape the data cannot
        // support.
        shape = L.circle(centroid, {
          pane: 'pug-ranges',
          radius: isFocused ? 1800 : 1200,
          className: `hull hull-aura ${isFocused ? 'focus' : ''}`,
          color: color.stroke, weight: isFocused ? 2.5 : 1.2, dashArray: '4 3',
          fillColor: color.fill, fillOpacity: isFocused ? 0.35 : isDimmed ? 0.04 : 0.15,
        });
      }
      if (shape) {
        shape.bindTooltip(hullTip(), tip());
        shape.on('click', (e) => {
          lastLayerClick = Date.now();
          L.DomEvent.stopPropagation(e);
          openTigerDrawer(o.ind_id, o, alertsByInd[o.ind_id] || []);
          onFocus?.(focus === o.ind_id ? null : o.ind_id);
        });
        shape.addTo(groups.ranges);
      }

      if (hasCentroid) {
        const dot = L.circleMarker(centroid, {
          pane: 'pug-ranges',
          className: 'centroid-group',
          radius: isFocused ? 7 : 4,
          color: '#ffffff', weight: isFocused ? 2 : 1.2,
          fillColor: color.stroke, fillOpacity: isDimmed ? 0.25 : 1,
        });
        dot.bindTooltip(hullTip(), tip());
        dot.on('click', (e) => {
          lastLayerClick = Date.now();
          L.DomEvent.stopPropagation(e);
          openTigerDrawer(o.ind_id, o, alertsByInd[o.ind_id] || []);
          onFocus?.(focus === o.ind_id ? null : o.ind_id);
        });
        dot.addTo(groups.ranges);

        if (isFocused) {
          dot.bindTooltip(esc(o.ind_id), { permanent: true, direction: 'top',
                                           className: 'hull-label-tip', opacity: 1 });
        }
      }
    });

    /* ── 4. movement vectors ─────────────────────────────────────────── */
    const priorById = Object.fromEntries(prior.map(p => [p.ind_id, p]));

    // On the whole-reserve view, a labelled arrow for every individual is
    // unreadable at real scale -- 150 individuals means 150 overlapping
    // arrows fighting for the same small map, which is exactly what buries
    // the ones that matter. Only individuals with a real alert get an arrow
    // on that view: "genuinely actionable rather than noisy" is the problem
    // statement's own evaluation criterion. Clicking into one tiger still
    // always shows its own movement, alert or not -- a deliberate drill-down
    // is not the crowded case this guards against.
    const movementSet = focus
      ? focused
      : occupancy.filter(o => (alertsByInd[o.ind_id] || []).length > 0);

    movementSet.forEach(o => {
      const was = priorById[o.ind_id];
      if (!was) return;
      const from = latlng(was.centroid_lat, was.centroid_lon);
      const to = latlng(o.centroid_lat, o.centroid_lon);
      // no centroid at either end is not an error: it means the cycle had too
      // few captures to place one. There is simply no movement to draw.
      if (!from || !to) return;
      const km = from.distanceTo(to) / 1000;
      if (km < 0.3) return;

      const indAlerts = alertsByInd[o.ind_id] || [];
      const hasVillageAlert = indAlerts.some(
        a => a.type === 'decreasing_village_distance' || a.type === 'buffer_ward');
      const color = getTigerColor(o.ind_id);
      const arrowColor = hasVillageAlert ? '#ef4444' : color.stroke;

      L.polyline([from, to], {
        pane: 'pug-movement',
        className: `shift ${hasVillageAlert ? 'shift-alert' : ''}`,
        color: arrowColor, weight: hasVillageAlert ? 3 : 2, opacity: 0.95,
      }).bindTooltip(
        `<b>${esc(o.ind_id)}</b> moved ${km.toFixed(1)} km` +
        (hasVillageAlert ? ' <span style="color:#ef4444">toward a village</span>' : ''),
        tip()).addTo(groups.movement);

      // where it was: hollow, so direction reads without needing the label
      L.circleMarker(from, {
        pane: 'pug-movement',
        className: 'was', radius: 4, color: arrowColor, weight: 1.5,
        fillColor: '#0b1a12', fillOpacity: 0.9,
      }).addTo(groups.movement);

      // Web Mercator is conformal, so the screen bearing between two points
      // is the same at every zoom -- the arrowhead can be rotated once here
      // and never needs recomputing on zoom.
      const p1 = map.project(from, 12), p2 = map.project(to, 12);
      const deg = Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180 / Math.PI;
      L.marker(to, {
        pane: 'pug-movement',
        interactive: false,
        icon: L.divIcon({
          className: 'shift-arrow',
          html: `<span style="transform:rotate(${deg.toFixed(1)}deg);color:${arrowColor}">➤</span>`,
          iconSize: [18, 18], iconAnchor: [9, 9],
        }),
      }).addTo(groups.movement);

      L.marker(L.latLng((from.lat + to.lat) / 2, (from.lng + to.lng) / 2), {
        pane: 'pug-movement',
        interactive: false,
        icon: L.divIcon({
          className: 'shift-label',
          html: `<span style="color:${arrowColor}">${hasVillageAlert ? '⚠ ' : ''}${km.toFixed(1)} km</span>`,
          iconSize: [72, 16], iconAnchor: [36, 18],
        }),
      }).addTo(groups.movement);
    });

    /* ── 5. camera stations ──────────────────────────────────────────── */
    usable.forEach(s => {
      const st = stationState(s);
      const inFocus = focus && focused.some(o => (o.station_set || []).includes(s.station_id));
      const visitedTigers = tigersByStation[s.station_id] || [];
      const r = inFocus ? 7 : (st === 'offline' || st === 'new') ? 6 : 4.5;

      const tigerList = visitedTigers.length
        ? visitedTigers.map(t => `<span class="tag-badge" style="background:${getTigerColor(t).fill};color:#fff">${esc(t)}</span>`).join(' ')
        : '<span style="color:#8aa398">None recorded</span>';

      const marker = L.circleMarker(latlng(s.lat, s.lon), {
        pane: 'pug-stations',
        // the class carries the theme: `.pugmap .stn-offline` etc. in
        // app.css already define the four states for light and dark
        className: `stn stn-${st} ${inFocus ? 'in-range' : ''}`,
        radius: r, weight: 1.4, fillOpacity: 1,
      });

      marker.bindTooltip(`
        <h4>📷 ${esc(s.name || s.station_id)}</h4>
        <div class="meta-line"><b>Zone:</b> ${esc((s.zone || 'reserve').toUpperCase())} &nbsp;|&nbsp; <b>ID:</b> ${esc(s.station_id)}</div>
        <div class="meta-line"><b>Status:</b> ${STATE_COPY[st] || st}</div>
        <div class="meta-line"><b>Village distance:</b> ${esc(s.village_dist_km ?? '—')} km</div>
        <div class="tip-sep"><b>Tigers seen here:</b><div style="margin-top:3px">${tigerList}</div></div>`,
        tip());

      marker.on('click', (e) => {
        lastLayerClick = Date.now();
        L.DomEvent.stopPropagation(e);
        openStationDrawer({
          station: s.station_id, name: s.name || s.station_id,
          zone: s.zone || 'reserve', state: st,
          village: s.village_dist_km ?? '—',
          camera: `${s.camera_make || ''} ${s.camera_model || ''}`.trim(),
          serial: s.camera_serial || '',
          tigers: visitedTigers.join(', '),
        }, s);
      });
      marker.addTo(groups.stations);
    });

    /* ── 5b. the focused tiger, drawn unconditionally ────────────────────
       "Locate on map" from the catalogue sets a focus and jumps here. Until
       now the only thing that drew for a focused individual was its hull or
       centroid, and both live in the Home Ranges layer -- which is OFF by
       default. So the map flew to the right place and showed nothing, and
       for a tiger seen at a single station there is no hull to draw at all
       (three stations are needed for a polygon), which is every individual
       in a first import.

       Focus is an explicit request for one animal, not a layer preference,
       so it is drawn in its own always-on pane. */
    if (focus) {
      const target = occupancy.find(o => o.ind_id === focus);
      const at = target && latlng(target.centroid_lat, target.centroid_lon);
      const colour = getTigerColor(focus);
      if (at) {
        L.circleMarker(at, {
          pane: 'pug-focus', className: 'focus-halo', interactive: false,
          radius: 22, color: colour.stroke, weight: 2, opacity: 0.9,
          fillColor: colour.fill, fillOpacity: 0.12,
        }).addTo(groups.focus);
        L.circleMarker(at, {
          pane: 'pug-focus', className: 'focus-pin', interactive: false,
          radius: 7, color: '#ffffff', weight: 2,
          fillColor: colour.stroke, fillOpacity: 1,
        }).addTo(groups.focus);
        L.marker(at, {
          pane: 'pug-focus', interactive: false,
          icon: L.divIcon({
            className: 'focus-label',
            html: '<span style="--c:' + colour.stroke + '">' + esc(focus) + '</span>',
            iconSize: [160, 18], iconAnchor: [80, 34],
          }),
        }).addTo(groups.focus);
      } else if (target) {
        // No centroid: the cycle produced no locatable capture for this
        // animal. Say so rather than flying to nowhere (rule 8).
        const note = document.getElementById('mapImageryNote');
        if (note) {
          note.textContent = focus + ' has no mapped position this cycle';
          note.classList.add('show');
        }
      }
    }

    /* ── 6. view: fit once, then leave the user's view alone ─────────── */
    if (!homeBounds) {
      homeBounds = L.latLngBounds(usable.map(s => [num(s.lat), num(s.lon)]));
      map.fitBounds(homeBounds, { padding: [40, 40] });
    }
    // A focus change is a deliberate "show me this one", so it -- and only
    // it -- is allowed to move the camera.
    if (focus && focus !== lastFocus) {
      const target = occupancy.find(o => o.ind_id === focus);
      const pts = target && parseHull(target.hull_wkt);
      if (pts && pts.length >= 3) {
        map.flyToBounds(L.latLngBounds(pts.map(p => [p.lat, p.lon])),
                        { padding: [60, 60], duration: 0.6, maxZoom: 14 });
      } else if (target && latlng(target.centroid_lat, target.centroid_lon)) {
        // A tiger at one station has no hull. Zoom to 14 rather than
        // "whatever we were on", or locating it from the catalogue leaves
        // the reserve at country scale with a dot somewhere in it.
        map.flyTo(latlng(target.centroid_lat, target.centroid_lon),
                  Math.max(map.getZoom(), 14), { duration: 0.6 });
      }
    }
    lastFocus = focus;

    /* ── 7. legend, controls, playback ───────────────────────────────── */
    const legendHost = document.getElementById('mapLegendHost');
    if (legendHost) {
      legendHost.innerHTML = renderLegend(occupancy, alertsByInd, focus);
      legendHost.querySelectorAll('[data-ind]').forEach(el =>
        el.addEventListener('click', () => onFocus?.(focus === el.dataset.ind ? null : el.dataset.ind)));
    }

    wireLayerButtons();
    wireZoomButtons();
    wireFullscreen();
    wirePlayback(events, rawStations, focus);
    wireSidebarToggle();
    syncImagery();
  }

  /* ── controls ─────────────────────────────────────────────────────── */

  function wireLayerButtons() {
    document.querySelectorAll('.map-layer-btn').forEach(btn => {
      const layer = btn.dataset.layer;
      if (!layer) return;
      btn.classList.toggle('active', !!viewState.layers[layer]);
      btn.onclick = () => {
        viewState.layers[layer] = !viewState.layers[layer];
        btn.classList.toggle('active', viewState.layers[layer]);
        if (layer === 'corridors') { refresh(); return; }   // redraw, not a group
        setLayerVisible(layer, viewState.layers[layer]);
        if (layer === 'basemap') syncImagery();
      };
    });
  }

  function wireZoomButtons() {
    const zin = document.getElementById('mapZoomInBtn');
    const zout = document.getElementById('mapZoomOutBtn');
    const zreset = document.getElementById('mapZoomResetBtn');
    if (zin) zin.onclick = () => map?.zoomIn();
    if (zout) zout.onclick = () => map?.zoomOut();
    if (zreset) zreset.onclick = () => {
      if (map && homeBounds) map.fitBounds(homeBounds, { padding: [40, 40] });
    };
  }

  function wireFullscreen() {
    const fsBtn = document.getElementById('mapFullscreenBtn');
    const wrap = document.getElementById('mapWrap');
    if (!fsBtn || !wrap) return;
    const card = wrap.closest('.map-canvas-card') || wrap;

    const sync = () => {
      const on = !!document.fullscreenElement;
      card.classList.toggle('is-fullscreen', on);
      fsBtn.innerHTML = on ? '🗗' : '⛶';
      fsBtn.title = on ? 'Exit fullscreen' : 'Toggle fullscreen';
      // the container just changed size by a lot
      setTimeout(() => { try { map?.invalidateSize(); } catch (e) {} }, 120);
    };

    fsBtn.onclick = () => {
      if (!document.fullscreenElement) {
        (card.requestFullscreen || card.webkitRequestFullscreen)?.call(card);
      } else {
        document.exitFullscreen?.();
      }
    };
    document.onfullscreenchange = sync;
  }

  function wireSidebarToggle() {
    const btn = document.getElementById('mapToggleSidebarBtn');
    if (!btn) return;
    btn.onclick = () => {
      const layout = document.querySelector('.map-layout');
      const sidebar = document.querySelector('.map-sidebar');
      if (!layout || !sidebar) return;
      const collapsed = layout.classList.toggle('sidebar-collapsed');
      sidebar.classList.toggle('collapsed', collapsed);
      btn.classList.toggle('active', !collapsed);
      btn.title = collapsed ? 'Show roster sidebar' : 'Hide roster sidebar';
      // the map column just got wider
      setTimeout(() => { try { map?.invalidateSize(); } catch (e) {} }, 220);
    };
  }

  /* ── timeline scrubber and movement playback ──────────────────────── */

  /* ── the movement player ──────────────────────────────────────────────
     What this is FOR: showing where a tiger went during the cycle, in the
     order it went there. The first version could not answer that.

       * It mapped the slider to an INDEX into the sighting list, so the
         timeline was not a time axis. Halfway along meant "the median
         sighting", which on unevenly spaced captures is nowhere near
         halfway through the month.
       * It drew one dot and deleted the previous one, so a path was never
         visible -- the one thing a movement player exists to show.
       * It stepped through every tiger's sightings interleaved, so the dot
         teleported around the reserve changing identity, which is the
         opposite of following an animal.

     This version makes the slider real time, keeps a fading trail, joins
     consecutive sightings of the SAME tiger so the path is the thing you
     see, plays one tiger alone when one is focused, and names the animal,
     the station and the timestamp under the playhead. */

  const TRAIL = 14;            // sightings kept visible behind the playhead
  const PLAY_MS = 14000;       // a full cycle takes this long to play

  function wirePlayback(events, rawStations, focus) {
    const playBtn = document.getElementById('mapPlayMovementBtn');
    const slider = document.getElementById('mapTimelineSlider');
    const dateEl = document.getElementById('mapTimelineDate');
    const whoEl = document.getElementById('mapTimelineWho');
    const L = window.L;

    const stnById = {};
    rawStations.forEach(s => {
      if (s.station_id && num(s.lat) !== null && num(s.lon) !== null) stnById[s.station_id] = s;
    });

    // A focused tiger plays alone: that is the point of focusing one.
    const mine = events
      .filter(ev => ev && ev.station_id && stnById[ev.station_id])
      .filter(ev => !focus || ev.ind_id === focus);
    const all = mine
      .map(ev => ({ ind_id: ev.ind_id, station_id: ev.station_id, t: Date.parse(ev.started_at) }))
      .filter(ev => Number.isFinite(ev.t))
      .sort((a, b) => a.t - b.t);
    // Sightings that exist but carry no usable capture time. They cannot go
    // on a time axis -- but "nothing happened" and "24 tigers with no
    // timestamps" are different facts and must not read the same.
    const undated = mine.length - all.length;

    const t0 = all.length ? all[0].t : 0;
    const t1 = all.length ? all[all.length - 1].t : 0;
    const span = Math.max(1, t1 - t0);

    const fmt = (ms) => {
      const d = new Date(ms);
      const p = (n) => String(n).padStart(2, '0');
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
             '  ' + p(d.getHours()) + ':' + p(d.getMinutes());
    };

    function draw(pct) {
      if (!map || !groups) return;
      groups.playback.clearLayers();
      if (!map.hasLayer(groups.playback)) groups.playback.addTo(map);
      if (!all.length) {
        if (dateEl) {
          dateEl.textContent = undated
            ? 'No dated sightings'
            : 'No sightings this cycle';
        }
        if (whoEl) {
          whoEl.innerHTML = undated
            ? '<span class="tl-where">' + undated + ' sighting' + (undated === 1 ? '' : 's')
              + ' recorded, but the photos carry no date or time — nothing to play along a timeline.'
              + '</span>'
            : '';
        }
        return;
      }

      const now = t0 + span * (pct / 100);
      const seen = all.filter(e => e.t <= now);
      const trail = seen.slice(-TRAIL);

      // consecutive sightings of one tiger, joined: this is the movement
      const byInd = {};
      trail.forEach(e => { (byInd[e.ind_id] = byInd[e.ind_id] || []).push(e); });
      Object.keys(byInd).forEach(ind => {
        const list = byInd[ind];
        if (list.length < 2) return;
        L.polyline(list.map(e => [num(stnById[e.station_id].lat), num(stnById[e.station_id].lon)]), {
          pane: 'pug-playback', interactive: false,
          color: getTigerColor(ind).stroke, weight: 2, opacity: 0.55, dashArray: '4 4',
        }).addTo(groups.playback);
      });

      trail.forEach((e, i) => {
        const stn = stnById[e.station_id];
        const at_ = latlng(stn.lat, stn.lon);
        if (!at_) return;
        const age = (trail.length - 1 - i) / Math.max(1, TRAIL - 1);   // 0 = newest
        const colour = getTigerColor(e.ind_id);
        const newest = i === trail.length - 1;
        L.circleMarker(at_, {
          pane: 'pug-playback', interactive: false,
          className: newest ? 'radar-ping' : 'radar-trail',
          radius: newest ? 7 : 4.5,
          color: colour.stroke, weight: newest ? 2.5 : 1.2,
          fillColor: colour.fill,
          fillOpacity: newest ? 0.95 : Math.max(0.12, 0.6 * (1 - age)),
          opacity: newest ? 1 : Math.max(0.15, 0.8 * (1 - age)),
        }).addTo(groups.playback);

        if (newest) {
          L.marker(at_, {
            pane: 'pug-playback', interactive: false,
            icon: L.divIcon({
              className: 'radar-ping-label',
              html: '<span style="color:' + colour.stroke + '">' + esc(e.ind_id) + '</span>',
              iconSize: [110, 16], iconAnchor: [-10, 20],
            }),
          }).addTo(groups.playback);
        }
      });

      const last = seen[seen.length - 1];
      if (dateEl) dateEl.textContent = fmt(now);
      if (whoEl) {
        if (last) {
          const stn = stnById[last.station_id];
          whoEl.innerHTML =
            '<span class="tl-chip" style="--c:' + getTigerColor(last.ind_id).fill + '">' +
              '<i></i>' + esc(last.ind_id) + '</span>' +
            '<span class="tl-where">' + esc((stn && stn.name) || last.station_id) + '</span>' +
            '<span class="tl-count">' + seen.length + ' of ' + all.length + ' sightings</span>';
        } else {
          whoEl.innerHTML = '<span class="tl-where">cycle begins — no sightings yet</span>';
        }
      }
    }

    function stop() {
      if (viewState.playbackTimer) {
        clearInterval(viewState.playbackTimer);
        viewState.playbackTimer = null;
      }
      if (playBtn) {
        playBtn.innerHTML = '▶ Play movement';
        playBtn.classList.remove('active');
      }
    }
    // A re-render (new data, new focus) must not leave an old interval
    // running against a layer group that has since been cleared.
    stop();

    if (slider) {
      slider.disabled = !all.length;
      slider.oninput = () => { stop(); draw(parseFloat(slider.value)); };
    }

    if (playBtn) {
      playBtn.disabled = !all.length;
      playBtn.title = all.length
        ? (focus ? 'Play ' + focus + ' through the cycle'
                 : 'Play every tiger through the cycle')
        : (undated
            ? undated + ' sighting(s) have no timestamp, so there is no timeline to play'
            : 'No sightings in this cycle to play');
      playBtn.onclick = () => {
        if (viewState.playbackTimer) { stop(); return; }
        if (!all.length) return;
        playBtn.innerHTML = '⏸ Pause';
        playBtn.classList.add('active');
        // start over if the playhead is already parked at the end
        let pct = (slider && parseFloat(slider.value) < 99.5) ? parseFloat(slider.value) : 0;
        const stepMs = 40;
        const per = 100 / (PLAY_MS / stepMs);
        viewState.playbackTimer = setInterval(() => {
          pct += per;
          if (pct >= 100) {
            pct = 100;
            if (slider) slider.value = pct;
            draw(pct);
            stop();
            return;
          }
          if (slider) slider.value = pct;
          draw(pct);
        }, stepMs);
      };
    }

    draw(slider ? parseFloat(slider.value) : 100);
  }

  /* ── drawers ──────────────────────────────────────────────────────── */

  function openStationDrawer(dataset) {
    const drawer = document.getElementById('mapSideDrawer');
    if (!drawer) return;
    drawer.innerHTML = `
      <div class="drawer-header">
        <h3>📷 Station: ${esc(dataset.name)}</h3>
        <button class="btn btn-ghost" data-close-drawer title="Close" aria-label="Close">✕</button>
      </div>
      <div class="drawer-body">
        <div class="meta-card">
          <div><b>Station ID:</b> ${esc(dataset.station)}</div>
          <div><b>Zone:</b> <span class="tag-badge">${esc((dataset.zone || 'reserve').toUpperCase())}</span></div>
          <div><b>Status:</b> ${STATE_COPY[dataset.state] || esc(dataset.state)}</div>
          <div><b>Village distance:</b> ${esc(dataset.village)} km</div>
        </div>
        ${dataset.camera ? `
        <div class="meta-card" style="margin-top:10px">
          <h4>Camera body</h4>
          <div><b>Make &amp; model:</b> ${esc(dataset.camera)}</div>
          ${dataset.serial ? `<div><b>Serial:</b> <code>${esc(dataset.serial)}</code></div>` : ''}
        </div>` : ''}
        <div class="meta-card" style="margin-top:10px">
          <h4>Tigers observed here</h4>
          <div>${dataset.tigers
            ? dataset.tigers.split(', ').map(t =>
                `<span class="badge" style="background:${getTigerColor(t).fill};color:#fff;margin:2px">${esc(t)}</span>`).join(' ')
            : 'No tigers recorded yet'}</div>
        </div>
      </div>`;
    drawer.classList.add('open');
  }

  function openTigerDrawer(ind_id, occObj, alertList) {
    const drawer = document.getElementById('mapSideDrawer');
    if (!drawer) return;
    const color = getTigerColor(ind_id);
    drawer.innerHTML = `
      <div class="drawer-header">
        <h3 style="color:${color.stroke}">🐅 Tiger: ${esc(ind_id)}</h3>
        <button class="btn btn-ghost" data-close-drawer title="Close" aria-label="Close">✕</button>
      </div>
      <div class="drawer-body">
        <div class="meta-card">
          <div><b>Home range:</b> ${occObj?.area_km2 || '—'} km²</div>
          <div><b>Sightings:</b> ${occObj?.event_count || 0} visits across ${(occObj?.station_set || []).length} stations</div>
          <div><b>Camera effort:</b> ${occObj?.effort_days || '—'} camera-days</div>
          ${num(occObj?.centroid_lat) !== null && num(occObj?.centroid_lon) !== null
            ? `<div><b>Centroid:</b> ${Number(occObj.centroid_lat).toFixed(4)}, ${Number(occObj.centroid_lon).toFixed(4)}</div>` : ''}
        </div>
        ${alertList.length ? `
        <div class="meta-card" style="margin-top:10px;border-left:3px solid var(--act)">
          <h4 style="color:var(--act)">Active alerts (${alertList.length})</h4>
          ${alertList.map(a => `
            <div style="margin-bottom:8px">
              <b>${esc(String(a.type).toUpperCase())}:</b> ${esc(a.what_changed)}
              <div style="font-size:11px;color:var(--muted)">Severity: <span class="badge badge-${esc(a.severity)}">${esc(a.severity)}</span></div>
            </div>`).join('')}
        </div>` : '<div class="meta-card" style="margin-top:10px;color:var(--info)">✓ No active alerts for this individual</div>'}
      </div>`;
    drawer.classList.add('open');
  }

  function renderLegend() {
    return `
      <div class="legend">
        <div class="keys">
          <span><i class="k stn-active"></i> Working &amp; recording</span>
          <span><i class="k stn-idle"></i> Working, 0 captures</span>
          <span><i class="k stn-offline"></i> Camera offline</span>
          <span><i class="k stn-new"></i> Installed this cycle</span>
          <span><i class="k k-hull"></i> Territory range</span>
          <span><i class="k k-shift"></i> Movement since last cycle</span>
          <span><i class="k" style="background:#ef4444"></i> Moving toward a village</span>
        </div>
      </div>`;
  }

  /* Kept because app.js's roster rows call it on hover. It was never
     exported before, so that call had silently done nothing. */
  function showTooltip(html, clientX, clientY) {
    const tooltip = document.getElementById('mapTooltip');
    const wrap = document.getElementById('mapWrap');
    if (!tooltip || !wrap) return;
    const rect = wrap.getBoundingClientRect();
    tooltip.innerHTML = html;
    tooltip.style.left = `${Math.max(80, Math.min(rect.width - 80, clientX - rect.left))}px`;
    tooltip.style.top = `${Math.max(60, clientY - rect.top)}px`;
    tooltip.classList.add('visible');
    clearTimeout(showTooltip._t);
    showTooltip._t = setTimeout(() => tooltip.classList.remove('visible'), 2600);
  }

  // the manifest arrives after this module is defined; redraw once it does
  basemapReady.then(() => { if (lastRender) refresh(); });

  return {
    render, refresh, showTooltip,
    makeProjection, niceDistance, parseHull, stationState, getTigerColor,
    get map() { return map; },
  };
})();
