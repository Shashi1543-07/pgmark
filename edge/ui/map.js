/* PUGMARK · Interactive GIS Map Renderer
   ─────────────────────────────────────────────────────────────────────────
   Modern interactive GIS canvas:
   - Pan (mouse drag) & Zoom (wheel / pinch / on-screen buttons)
   - Distinct color palette for individual tigers
   - Interactive camera inspection & territory hover cards
   - Dynamic layer toggles (Ranges, Cameras, Zones, Movement, Density)
   - 100% offline (Pure SVG + CSS + JS, zero external tiles or CDNs)
   ───────────────────────────────────────────────────────────────────────── */

window.PugMap = (() => {
  const NS = 'http://www.w3.org/2000/svg';

  // Distinct harmonious colors for individual tiger territories
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

  /* ── projection ──────────────────────────────────────────────────────── */

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

  /* ── Pan & Zoom Viewport State ────────────────────────────────────────── */
  let viewState = {
    x: 0,
    y: 0,
    k: 1.0,
    isPanning: false,
    startX: 0,
    startY: 0,
    layers: {
      ranges: true,
      stations: true,
      zones: true,
      movement: true,
      heatmap: false,
    }
  };

  /**
   * Main render function for the interactive map
   */
  function render(host, data, onFocus) {
    if (!host) return;
    const rawStations = data?.stations || [];
    const occupancy = Array.isArray(data?.occupancy) ? data.occupancy : [];
    const prior = Array.isArray(data?.prior) ? data.prior : [];
    const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
    const focus = data?.focus || null;

    const usable = rawStations.filter(s => s && isFinite(Number(s.lat)) && isFinite(Number(s.lon)));
    if (!usable.length) {
      host.innerHTML = `<div class="card empty" style="padding:var(--s4);text-align:center">
        <strong>No mapped stations</strong><br>
        This reserve has no camera stations with coordinates recorded.</div>`;
      return;
    }

    const W = 900, H = 560, PAD = 48;
    const proj = makeProjection(usable, W, H, PAD);
    if (!proj) {
      host.innerHTML = `<div class="card empty">Could not calculate coordinate projection for stations.</div>`;
      return;
    }
    const P = (lat, lon) => proj.project(lat, lon);

    const focused = focus ? occupancy.filter(o => o.ind_id === focus) : occupancy;
    const alertsByInd = alerts.reduce((m, a) => {
      if (a && a.ind_id) {
        (m[a.ind_id] = m[a.ind_id] || []).push(a);
      }
      return m;
    }, {});

    // Map stations to visited tigers lookup
    const tigersByStation = {};
    occupancy.forEach(o => {
      (o.station_set || []).forEach(stnId => {
        tigersByStation[stnId] = tigersByStation[stnId] || [];
        tigersByStation[stnId].push(o.ind_id);
      });
    });

    /* ── 1. Coordinate Grid Lines & Elevation Background ─────────────── */
    let gridSvg = '<g class="grid-layer">';
    const gridCols = 8, gridRows = 6;
    for (let c = 1; c < gridCols; c++) {
      const gx = (W / gridCols) * c;
      gridSvg += `<line x1="${gx}" y1="0" x2="${gx}" y2="${H}" class="grid-line" stroke-dasharray="2 4"/>`;
    }
    for (let r = 1; r < gridRows; r++) {
      const gy = (H / gridRows) * r;
      gridSvg += `<line x1="0" y1="${gy}" x2="${W}" y2="${gy}" class="grid-line" stroke-dasharray="2 4"/>`;
    }
    gridSvg += '</g>';

    /* ── 2. Core & Buffer Reserve Zones ──────────────────────────────── */
    let zoneLayer = '<g class="zones-layer" style="' + (viewState.layers.zones ? '' : 'display:none') + '">';
    ['buffer', 'core'].forEach(zone => {
      const pts = usable.filter(s => (s.zone || '').toLowerCase() === zone).map(s => P(s.lat, s.lon));
      if (pts.length >= 3) {
        const hull = convexHull(pts);
        if (hull && hull.length >= 3) {
          const d = hull.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
          const count = usable.filter(s => (s.zone || '').toLowerCase() === zone).length;
          zoneLayer += `<polygon class="zone zone-${zone}" points="${d}" data-zone="${zone}" data-count="${count}"></polygon>`;
        }
      }
    });
    zoneLayer += '</g>';

    /* ── 3. Home Ranges / Alpha Hulls with Distinct Colors ───────────── */
    let hulls = '<g class="hulls-layer" style="' + (viewState.layers.ranges ? '' : 'display:none') + '">';
    let centroidBadges = '<g class="centroids-layer" style="' + (viewState.layers.ranges ? '' : 'display:none') + '">';

    occupancy.forEach(o => {
      const isFocused = focus === o.ind_id;
      const isDimmed = focus && !isFocused;
      const pts = parseHull(o.hull_wkt);
      const color = getTigerColor(o.ind_id);
      const flagged = alertsByInd[o.ind_id]?.some(a => a.severity === 'act');

      if (pts) {
        const d = pts.map(p => {
          const [x, y] = P(p.lat, p.lon);
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');

        const fillOpacity = isFocused ? 0.40 : isDimmed ? 0.05 : 0.18;
        const strokeWidth = isFocused ? 3.0 : isDimmed ? 1.0 : 1.8;
        const strokeColor = isFocused ? color.stroke : isDimmed ? '#a0aec0' : color.stroke;

        hulls += `<polygon class="hull ${isFocused ? 'focus' : ''} ${flagged ? 'flagged' : ''}"
          points="${d}"
          fill="${color.fill}"
          fill-opacity="${fillOpacity}"
          stroke="${strokeColor}"
          stroke-width="${strokeWidth}"
          data-ind="${esc(o.ind_id)}"
          data-area="${o.area_km2 || '—'}"
          data-events="${o.event_count || 0}"
          data-cams="${(o.station_set || []).length}"
          data-alerts="${alertsByInd[o.ind_id]?.length || 0}"
        ></polygon>`;

        // Centroid Badge
        if (isFinite(o.centroid_lat) && isFinite(o.centroid_lon)) {
          const [cx, cy] = P(o.centroid_lat, o.centroid_lon);
          centroidBadges += `
            <g class="centroid-group" data-ind="${esc(o.ind_id)}" style="cursor:pointer;opacity:${isDimmed ? 0.3 : 1}">
              <circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="4.5" fill="${color.stroke}" stroke="#fff" stroke-width="1.5"/>
              <text x="${cx.toFixed(1)}" y="${(cy - 8).toFixed(1)}" class="hull-label" fill="${color.stroke}">${esc(o.ind_id)}</text>
            </g>`;
        }
      }
    });
    hulls += '</g>';
    centroidBadges += '</g>';

    /* ── 4. Movement Vectors ─────────────────────────────────────────── */
    const priorById = Object.fromEntries(prior.map(p => [p.ind_id, p]));
    let shifts = '<g class="shifts-layer" style="' + (viewState.layers.movement ? '' : 'display:none') + '">';

    const movementSet = focus ? focused : occupancy;
    movementSet.forEach(o => {
      const was = priorById[o.ind_id];
      if (was && isFinite(was.centroid_lat) && isFinite(o.centroid_lat)) {
        const [x1, y1] = P(was.centroid_lat, was.centroid_lon);
        const [x2, y2] = P(o.centroid_lat, o.centroid_lon);
        const dist = Math.hypot(x2 - x1, y2 - y1);
        if (dist >= 6) {
          const km = (dist * proj.kmPerPx).toFixed(1);
          const color = getTigerColor(o.ind_id);
          shifts += `<g class="shift" data-ind="${esc(o.ind_id)}">
            <line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"
                  stroke="${color.stroke}" marker-end="url(#arrow-${esc(o.ind_id)})"/>
            <circle cx="${x1.toFixed(1)}" cy="${y1.toFixed(1)}" r="3" class="was"/>
            <text x="${((x1 + x2)/2).toFixed(1)}" y="${((y1 + y2)/2 - 5).toFixed(1)}" class="hull-label" fill="${color.stroke}">${km} km</text>
          </g>`;
        }
      }
    });
    shifts += '</g>';

    /* ── 5. Camera Stations & Heatmap Density ────────────────────────── */
    let heatmapLayer = '<g class="heatmap-layer" style="' + (viewState.layers.heatmap ? '' : 'display:none') + '">';
    let pins = '<g class="stations-layer" style="' + (viewState.layers.stations ? '' : 'display:none') + '">';

    usable.forEach(s => {
      const st = stationState(s);
      const [x, y] = P(s.lat, s.lon);
      const inFocus = focus && focused.some(o => (o.station_set || []).includes(s.station_id));
      const visitedTigers = tigersByStation[s.station_id] || [];
      const imgCount = s.image_count || 0;

      // Heatmap density halo
      if (imgCount > 0) {
        const heatRadius = Math.min(32, 8 + Math.sqrt(imgCount) * 2.5);
        heatmapLayer += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${heatRadius}"
          fill="rgba(245, 158, 11, 0.25)" stroke="rgba(217, 119, 6, 0.4)" stroke-width="1"/>`;
      }

      const r = inFocus ? 6.5 : (st === 'offline' || st === 'new') ? 5.5 : 4.0;
      pins += `<circle class="stn stn-${st} ${inFocus ? 'in-range' : ''}"
        cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}"
        data-station="${esc(s.station_id)}"
        data-name="${esc(s.name || s.station_id)}"
        data-zone="${esc(s.zone || 'reserve')}"
        data-state="${st}"
        data-frames="${imgCount}"
        data-tigers="${esc(visitedTigers.join(', '))}"
        tabindex="0"
      ></circle>`;
    });
    heatmapLayer += '</g>';
    pins += '</g>';

    /* ── 6. Dynamic Scalebar & North Arrow ───────────────────────────── */
    const barKm = niceDistance(proj.spanX / 4);
    const barPx = barKm / proj.kmPerPx;
    const scaleBar = `<g class="scalebar" transform="translate(${PAD}, ${H - 24})">
      <line x1="0" y1="0" x2="${barPx.toFixed(1)}" y2="0"/>
      <line x1="0" y1="-4" x2="0" y2="4"/>
      <line x1="${barPx.toFixed(1)}" y1="-4" x2="${barPx.toFixed(1)}" y2="4"/>
      <text x="${(barPx / 2).toFixed(1)}" y="-7" text-anchor="middle">${barKm} km</text>
    </g>`;

    const northArrow = `<g class="north" transform="translate(${W - 36}, 32)">
      <line x1="0" y1="16" x2="0" y2="-10"/>
      <path d="M0,-14 L4,-6 L-4,-6 Z"/>
      <text x="0" y="28" text-anchor="middle">N</text>
    </g>`;

    // Marker defs for arrowheads
    let arrowDefs = '<defs>';
    occupancy.forEach(o => {
      const color = getTigerColor(o.ind_id);
      arrowDefs += `<marker id="arrow-${esc(o.ind_id)}" viewBox="0 0 8 8" refX="7" refY="4"
        markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="${color.stroke}"/></marker>`;
    });
    arrowDefs += '</defs>';

    // Assemble SVG HTML
    host.innerHTML = `
      <svg id="mainPugSvg" viewBox="0 0 ${W} ${H}" class="pugmap" role="img">
        ${arrowDefs}
        ${gridSvg}
        <g id="mapViewport" class="viewport" transform="translate(${viewState.x}, ${viewState.y}) scale(${viewState.k})">
          ${zoneLayer}
          ${heatmapLayer}
          ${hulls}
          ${shifts}
          ${pins}
          ${centroidBadges}
        </g>
        ${scaleBar}
        ${northArrow}
      </svg>`;

    /* ── Render Legend & Attribution Below ───────────────────────────── */
    const legendHost = document.getElementById('mapLegendHost');
    if (legendHost) {
      legendHost.innerHTML = renderLegend(occupancy, alertsByInd, focus);
      legendHost.querySelectorAll('[data-ind]').forEach(el =>
        el.addEventListener('click', () => onFocus?.(focus === el.dataset.ind ? null : el.dataset.ind)));
    }

    /* ── Wire Interactive Tooltip & Hover Card ───────────────────────── */
    const tooltip = document.getElementById('mapTooltip');
    const svgEl = document.getElementById('mainPugSvg');
    const wrap = document.getElementById('mapWrap');

    function showTooltip(html, clientX, clientY) {
      if (!tooltip || !wrap) return;
      tooltip.innerHTML = html;
      const rect = wrap.getBoundingClientRect();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      tooltip.style.left = `${Math.max(80, Math.min(rect.width - 80, x))}px`;
      tooltip.style.top = `${Math.max(60, y)}px`;
      tooltip.classList.add('visible');
    }

    function hideTooltip() {
      if (tooltip) tooltip.classList.remove('visible');
    }

    // Station Hover
    host.querySelectorAll('.stn').forEach(stn => {
      stn.addEventListener('mouseenter', (e) => {
        const d = stn.dataset;
        const tigerList = d.tigers ? d.tigers.split(', ').map(t => `<span class="tag-badge" style="background:#d97706;color:#fff">${t}</span>`).join(' ') : '<span style="color:#888">None recorded</span>';
        const html = `
          <h4>📷 ${esc(d.name)}</h4>
          <div class="meta-line"><b>Zone:</b> ${esc((d.zone || 'reserve').toUpperCase())} &nbsp;|&nbsp; <b>ID:</b> ${esc(d.station)}</div>
          <div class="meta-line"><b>Status:</b> ${STATE_COPY[d.state] || d.state}</div>
          <div class="meta-line"><b>Total Frames:</b> ${d.frames} photos</div>
          <div style="margin-top:6px;border-top:1px solid rgba(255,255,255,0.15);padding-top:4px">
            <b>Tigers Seen Here:</b><div style="margin-top:3px">${tigerList}</div>
          </div>`;
        showTooltip(html, e.clientX, e.clientY);
      });
      stn.addEventListener('mouseleave', hideTooltip);
      stn.addEventListener('click', (e) => {
        const stnId = stn.dataset.station;
        const tigers = tigersByStation[stnId];
        if (tigers && tigers.length) {
          onFocus?.(tigers[0]);
        }
      });
    });

    // Hull Hover
    host.querySelectorAll('.hull, .centroid-group').forEach(hull => {
      hull.addEventListener('mouseenter', (e) => {
        const ind = hull.dataset.ind;
        const o = occupancy.find(item => item.ind_id === ind);
        if (!o) return;
        const color = getTigerColor(ind);
        const alertsList = alertsByInd[ind] || [];
        const alertHtml = alertsList.length ? `<div style="color:#ef4444;margin-top:4px">⚠ ${alertsList.length} alert(s) on territory</div>` : '';
        const html = `
          <h4 style="color:${color.fill}">🐅 ${esc(ind)}</h4>
          <div class="meta-line"><b>Territory Area:</b> ${o.area_km2 || '—'} km²</div>
          <div class="meta-line"><b>Sightings:</b> ${o.event_count || 0} visits across ${(o.station_set || []).length} cameras</div>
          <div class="meta-line"><b>Camera Effort:</b> ${o.effort_days || '—'} camera-days</div>
          ${alertHtml}
          <div style="font-size:10.5px;color:#aaa;margin-top:6px;font-style:italic">Click to isolate and inspect territory</div>`;
        showTooltip(html, e.clientX, e.clientY);
      });
      hull.addEventListener('mouseleave', hideTooltip);
      hull.addEventListener('click', (e) => {
        onFocus?.(focus === hull.dataset.ind ? null : hull.dataset.ind);
      });
    });

    /* ── Wire Pan & Drag Behavior ────────────────────────────────────── */
    if (wrap) {
      wrap.onmousedown = (e) => {
        if (e.target.tagName === 'BUTTON' || e.target.closest('.map-zoom-bar') || e.target.closest('.map-layer-bar')) return;
        viewState.isPanning = true;
        viewState.startX = e.clientX - viewState.x;
        viewState.startY = e.clientY - viewState.y;
        wrap.classList.add('panning');
      };

      window.onmousemove = (e) => {
        if (!viewState.isPanning) return;
        viewState.x = e.clientX - viewState.startX;
        viewState.y = e.clientY - viewState.startY;
        const vp = document.getElementById('mapViewport');
        if (vp) {
          vp.setAttribute('transform', `translate(${viewState.x}, ${viewState.y}) scale(${viewState.k})`);
        }
      };

      window.onmouseup = () => {
        if (viewState.isPanning) {
          viewState.isPanning = false;
          wrap.classList.remove('panning');
        }
      };

      // Mouse Wheel Zoom
      wrap.onwheel = (e) => {
        e.preventDefault();
        const delta = e.deltaY < 0 ? 1.15 : 0.87;
        const newK = Math.max(0.6, Math.min(8.0, viewState.k * delta));
        const rect = wrap.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        viewState.x = mouseX - (mouseX - viewState.x) * (newK / viewState.k);
        viewState.y = mouseY - (mouseY - viewState.y) * (newK / viewState.k);
        viewState.k = newK;

        const vp = document.getElementById('mapViewport');
        if (vp) {
          vp.setAttribute('transform', `translate(${viewState.x}, ${viewState.y}) scale(${viewState.k})`);
        }
      };
    }

    /* ── Wire Floating Zoom Buttons ──────────────────────────────────── */
    const zoomInBtn = document.getElementById('mapZoomInBtn');
    const zoomOutBtn = document.getElementById('mapZoomOutBtn');
    const zoomResetBtn = document.getElementById('mapZoomResetBtn');

    if (zoomInBtn) zoomInBtn.onclick = () => zoomBy(1.25);
    if (zoomOutBtn) zoomOutBtn.onclick = () => zoomBy(0.8);
    if (zoomResetBtn) zoomResetBtn.onclick = () => resetZoom();

    function zoomBy(factor) {
      viewState.k = Math.max(0.6, Math.min(8.0, viewState.k * factor));
      const vp = document.getElementById('mapViewport');
      if (vp) {
        vp.setAttribute('transform', `translate(${viewState.x}, ${viewState.y}) scale(${viewState.k})`);
      }
    }

    function resetZoom() {
      viewState.x = 0;
      viewState.y = 0;
      viewState.k = 1.0;
      const vp = document.getElementById('mapViewport');
      if (vp) {
        vp.setAttribute('transform', `translate(0, 0) scale(1)`);
      }
    }

    /* ── Wire Layer Toggle Buttons ───────────────────────────────────── */
    document.querySelectorAll('.map-layer-btn').forEach(btn => {
      const layer = btn.dataset.layer;
      btn.onclick = () => {
        viewState.layers[layer] = !viewState.layers[layer];
        btn.classList.toggle('active', viewState.layers[layer]);
        // Update layer group visibility
        const grp = host.querySelector(`.${layer === 'ranges' ? 'hulls-layer' : layer === 'stations' ? 'stations-layer' : layer === 'zones' ? 'zones-layer' : layer === 'movement' ? 'shifts-layer' : 'heatmap-layer'}`);
        if (grp) grp.style.display = viewState.layers[layer] ? '' : 'none';
        const centGrp = host.querySelector('.centroids-layer');
        if (centGrp && layer === 'ranges') centGrp.style.display = viewState.layers.ranges ? '' : 'none';
      };
    });
  }

  /* ── Legend Renderer ─────────────────────────────────────────────────── */
  function renderLegend(occupancy, alertsByInd, focus) {
    return `
      <div class="legend">
        <div class="keys">
          <span><i class="k stn-active"></i> 🟢 Working & Recording</span>
          <span><i class="k stn-idle"></i> ⚪ Working (0 captures)</span>
          <span><i class="k stn-offline"></i> 🔴 Camera Offline / Stopped</span>
          <span><i class="k stn-new"></i> 🟡 New Camera</span>
          <span><i class="k k-hull"></i> 🐅 Territory Range</span>
          <span><i class="k k-shift"></i> ➜ Shift Vector</span>
        </div>
      </div>`;
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

  return { render, makeProjection, niceDistance, parseHull, stationState, getTigerColor };
})();
