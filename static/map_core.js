/* map_core.js — shared map machinery for every Metal Mapper view.
 *
 * Loaded after leaflet.js; exposes one global: MM.
 * The engineering view (view.html) and the operator view (/run, planned)
 * both build on this so the coil math, colors, and status vocabulary can
 * never diverge between views.
 */
'use strict';

const MM = (() => {

  const M_PER_DEG_LAT = 111320.0;

  /* Coil positions for one antenna fix. MUST mirror coil_positions() in
   * db_map.py (the Python copy is the tested reference).
   *
   * Array model: 8 coils in a line perpendicular to travel (cross-track),
   * centered on the array center = antenna + (fore, right) offsets.
   * adc index i = coil i, port -> starboard: (i - 3.5) * spacing.
   * geom.rot_deg is added to the reported heading first — the detector GPS
   * may report heading along its antenna baseline instead of the direction
   * of travel, and this knob absorbs whatever the mounting turns out to be. */
  function coilPositions(lat, lon, headingDeg, geom) {
    const h = (headingDeg + (geom.rot_deg || 0)) * Math.PI / 180;
    const fwdN = Math.cos(h), fwdE = Math.sin(h);
    const rgtN = -Math.sin(h), rgtE = Math.cos(h);
    const foreM = geom.fore_mm / 1000;
    const mPerDegLon = M_PER_DEG_LAT * Math.cos(lat * Math.PI / 180);
    const out = [];
    for (let i = 0; i < 8; i++) {
      const rightM = (geom.right_mm + (i - 3.5) * geom.spacing_mm) / 1000;
      const dN = foreM * fwdN + rightM * rgtN;
      const dE = foreM * fwdE + rightM * rgtE;
      out.push([lat + dN / M_PER_DEG_LAT, lon + dE / mPerDegLon]);
    }
    return out;
  }

  /* Geometry from a study meta dict (strings), with safe defaults.
   * rot_deg comes from the per-study view setting, not the hardware. */
  function geomFromMeta(meta) {
    const rot   = parseFloat(meta.sl_heading_offset_deg);
    const right = parseFloat(meta.coil_offset_right_mm);
    return {
      spacing_mm: parseFloat(meta.coil_spacing_mm) || 330,
      fore_mm:    parseFloat(meta.coil_offset_fore_mm) || 0,
      right_mm:   Number.isFinite(right) ? right : 500,
      rot_deg:    Number.isFinite(rot) ? rot : 270,
    };
  }

  /* Green(below zero) -> grey(at zero) -> red(above zero), in raw ADC
   * counts. zero is per channel; offset/ranges are shared across channels. */
  function colorFor(val, zero, offset, plusRange, minusRange) {
    if (zero == null) return 'rgb(128,128,128)';
    const z = zero + offset;
    if (val <= z) {
      const n = minusRange > 0
        ? Math.max(0, Math.min(1, (val - (z - minusRange)) / minusRange)) : 0;
      return `rgb(${Math.round(n * 128)},${Math.round(255 - n * 127)},${Math.round(n * 128)})`;
    }
    const n = plusRange > 0
      ? Math.max(0, Math.min(1, (val - z) / plusRange)) : 0;
    return `rgb(${Math.round(128 + n * 127)},${Math.round(128 - n * 128)},${Math.round(128 - n * 128)})`;
  }

  /* GGA fix quality vocabulary — same everywhere. */
  const FIX_LABELS = {0: 'No Fix', 1: 'GPS', 2: 'DGPS', 3: 'PPS',
                      4: 'RTK Fixed', 5: 'RTK Float'};
  function fixLabel(q) {
    return (q == null) ? 'No Fix' : (FIX_LABELS[q] || `Fix ${q}`);
  }
  function fixColor(q) {
    if (q === 4) return '#44ff44';
    if (q === 5 || q === 2) return '#ffee00';
    if (q == null || q === 0) return '#ff4444';
    return '#ffaa00';
  }

  /* Operator-friendly text for the daemon's live.reason vocabulary. */
  function reasonText(reason) {
    if (!reason) return '';
    if (reason.startsWith('fix=')) {
      return `GPS fix quality ${reason.slice(4)} — waiting for RTK fixed`;
    }
    return {
      'no heading': 'RTK fixed — waiting for dual-antenna heading',
      'no fix':     'no GPS fix (indoors / no sky view?)',
      'no data':    'no data from detector — check cable & power',
      'garbage':    'unreadable data — wrong port or baud?',
      'no port':    'serial port not found',
    }[reason] || reason;
  }

  /* ── Nav GPS marker (course-up arrow when moving, dot when not) ─────────── */

  const NAV_MIN_SPEED_MPS = 0.5;   // below this, COG is noise: show a dot

  function NavLayer(map) {
    let marker = null;
    let lastMode = null;

    function iconFor(nav) {
      const moving = (nav.speed_mps != null && nav.speed_mps >= NAV_MIN_SPEED_MPS
                      && nav.cog_deg != null);
      const color = nav.stale ? '#888' : fixColor(nav.quality);
      if (moving) {
        return {
          mode: 'arrow',
          icon: L.divIcon({
            className: 'nav-icon',
            html: `<div style="transform: rotate(${nav.cog_deg}deg);
                     width:0;height:0;margin:2px auto;
                     border-left:9px solid transparent;
                     border-right:9px solid transparent;
                     border-bottom:22px solid ${color};
                     filter: drop-shadow(0 0 2px #000);"></div>`,
            iconSize: [26, 26], iconAnchor: [13, 13],
          }),
        };
      }
      return {
        mode: 'dot',
        icon: L.divIcon({
          className: 'nav-icon',
          html: `<div style="width:14px;height:14px;border-radius:50%;
                   background:${color};border:2px solid #000;
                   margin:5px auto;"></div>`,
          iconSize: [26, 26], iconAnchor: [13, 13],
        }),
      };
    }

    return {
      update(nav) {
        if (!nav || !nav.connected || nav.lat == null || nav.lon == null) {
          if (marker) { map.removeLayer(marker); marker = null; lastMode = null; }
          return;
        }
        const {mode, icon} = iconFor(nav);
        if (!marker) {
          marker = L.marker([nav.lat, nav.lon], {icon, zIndexOffset: 1000})
                    .addTo(map);
        } else {
          marker.setLatLng([nav.lat, nav.lon]);
          marker.setIcon(icon);   // cheap; rebuilt every update for rotation
        }
        lastMode = mode;
        marker.bindTooltip(
          `NAV ${fixLabel(nav.quality)}` +
          (nav.speed_mps != null ? ` · ${(nav.speed_mps * 3.6).toFixed(1)} km/h` : ''),
          {permanent: false});
      },
      position() { return marker ? marker.getLatLng() : null; },
    };
  }

  /* ── Columnar point store ─────────────────────────────────────────────────
   * Typed arrays, one slot per point — no per-point JS objects. ~90 B/point,
   * so a 200k-point study is ~18 MB flat instead of GB-scale marker objects.
   * heading NaN = none (drift test / --no-gate); fix 255 = none. */
  function PointStore() {
    let cap = 1024, n = 0;
    let id  = new Uint32Array(cap);
    let lat = new Float64Array(cap), lon = new Float64Array(cap);
    let hdg = new Float32Array(cap);
    let fix = new Uint8Array(cap);
    let adc = new Uint16Array(cap * 8);        // interleaved, 8 per point

    function ensure(min) {
      if (min <= cap) return;
      while (cap < min) cap *= 2;
      const grow = (old, T, k) => { const a = new T(cap * k); a.set(old); return a; };
      id  = grow(id,  Uint32Array,  1);
      lat = grow(lat, Float64Array, 1);
      lon = grow(lon, Float64Array, 1);
      hdg = grow(hdg, Float32Array, 1);
      fix = grow(fix, Uint8Array,   1);
      adc = grow(adc, Uint16Array,  8);
    }

    return {
      push(p) {                                // one SSE row dict
        ensure(n + 1);
        id[n]  = p.id; lat[n] = p.lat; lon[n] = p.lon;
        hdg[n] = (p.heading == null) ? NaN : p.heading;
        fix[n] = (p.fix == null) ? 255 : p.fix;
        for (let ch = 0; ch < 8; ch++) adc[n * 8 + ch] = p.adc[ch];
        n++;
      },
      fillColumns(cols) {                      // bulk /data columns
        const m = cols.lat.length;
        ensure(n + m);
        for (let i = 0; i < m; i++) {
          id[n]  = cols.id[i]; lat[n] = cols.lat[i]; lon[n] = cols.lon[i];
          hdg[n] = (cols.heading[i] == null) ? NaN : cols.heading[i];
          fix[n] = (cols.fix[i] == null) ? 255 : cols.fix[i];
          for (let ch = 0; ch < 8; ch++) adc[n * 8 + ch] = cols.adc[ch][i];
          n++;
        }
      },
      get length() { return n; },
      view() { return {n, id, lat, lon, hdg, fix, adc}; },  // live refs: re-fetch after push
      adcRow(i) { return Array.from(adc.subarray(i * 8, i * 8 + 8)); },
      headingAt(i) { const h = hdg[i]; return Number.isNaN(h) ? null : h; },
      fixAt(i) { return fix[i] === 255 ? null : fix[i]; },
    };
  }

  /* ── 8-channel dot renderer: ONE canvas, zero per-dot objects ─────────────
   * Every redraw culls the store to the viewport, projects, and paints dots
   * directly (fillRect) — cost tracks dots IN VIEW, not study size. Slider /
   * calib changes just repaint. Zoomed way out it switches to peak-preserving
   * LOD: antenna positions bin to a coarse pixel grid and each cell draws one
   * dot colored by its strongest |value − zero| across enabled channels — a
   * heat map of detection strength that can never average away a hit.
   * Popups come from hitTest() (click → nearest dot), so EVERY dot is
   * inspectable, not just the newest ones. */
  function ChannelRenderer(map) {
    const canvas = L.DomUtil.create('canvas',
                                    'leaflet-layer leaflet-zoom-hide');
    canvas.style.position = 'absolute';
    canvas.style.pointerEvents = 'none';
    map.getPanes().overlayPane.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    let store = null;
    const visible = new Array(8).fill(true);
    let calib = {zero8: null, offset: -140, plusRange: 450, minusRange: 450};
    let geom  = {spacing_mm: 500, fore_mm: 0, right_mm: 0, rot_deg: 0};
    let filter = 'abs';   // 'abs' | 'cm' (subtract the array's common mode)
    let combine = true;   // true: overlaps accumulate (re-pass adds);
                          // false: classic paint, newest dots cover older

    /* Common mode at point i: mean zeroed deviation across ENABLED channels
     * (enabled-only so a dead/railed coil can be toggled out of the mean).
     * Subtracting it hides anything that moves all coils together — drift,
     * ground response — leaving only differential (target-like) signal. */
    function cmOf(v, i, zero8) {
      let sum = 0, cnt = 0;
      for (let ch = 0; ch < 8; ch++) {
        if (!visible[ch]) continue;
        sum += v.adc[i * 8 + ch] - zero8[ch];
        cnt++;
      }
      return cnt ? sum / cnt : 0;
    }

    const D = Math.PI / 180;
    const LOD_POINTS = 30000;      // in-view points before binning kicks in
    const LOD_CELL   = 4;          // px; ~1 dot per cell when zoomed out
    const TRACK_MAX  = 50000;      // track vertices before striding
    const REVISIT_GAP = 100;       // points; nearer touches of a cell merge
                                   // (same visit), farther ones ADD (re-pass)

    /* Color LUT: 256 steps per side, built FROM colorFor so the mapping has
     * exactly one definition (the function Jeremy tunes). Index i = ramp
     * position; grey at the zero end, full green/red at the far end. */
    let lutBelow = [], lutAbove = [];
    const lutRGB = new Uint8Array(512 * 3);  // numeric mirror for ImageData:
                                             // 0..255 below, 256..511 above
    function rebuildLut() {
      lutBelow = []; lutAbove = [];
      for (let i = 0; i < 256; i++) {
        lutBelow.push(colorFor(i - 255, 0, 0, 255, 255)); // n = i/255 below
        lutAbove.push(colorFor(i,       0, 0, 255, 255)); // n = i/255 above
      }
      for (let i = 0; i < 512; i++) {
        const m = (i < 256 ? lutBelow[i] : lutAbove[i - 256])
          .match(/rgb\((\d+),(\d+),(\d+)\)/);
        lutRGB[i * 3] = +m[1]; lutRGB[i * 3 + 1] = +m[2];
        lutRGB[i * 3 + 2] = +m[3];
      }
    }
    rebuildLut();

    function styleFor(val, z, minus, plus) {
      if (val <= z) {
        let t = minus > 0 ? (val - (z - minus)) / minus : 0;
        if (t < 0) t = 0; else if (t > 1) t = 1;
        return lutBelow[(t * 255) | 0];
      }
      let t = plus > 0 ? (val - z) / plus : 0;
      if (t < 0) t = 0; else if (t > 1) t = 1;
      return lutAbove[(t * 255) | 0];
    }

    /* Reusable scratch buffers (grown, never shrunk; no per-frame allocs) */
    let visIdx = new Int32Array(4096);   // in-bbox point indices
    let cellIdx = new Int32Array(0);     // LOD: winning point per cell
    let cellDev = new Float32Array(0);   // LOD: its deviation
    let accVal  = new Float32Array(0);   // detail: settled sum of past visits
    let accSeg  = new Float32Array(0);   // detail: current visit's extreme
    let accLast = new Int32Array(0);     // detail: last point index per cell
    let accCanvas = null, accCtx = null, accImg = null;

    let dirty = false, raf = 0;
    function markDirty() {
      dirty = true;
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (dirty) redraw(); });
    }

    /* Cull the store to the padded viewport; returns count in visIdx. */
    function cull(v) {
      const b = map.getBounds();
      const spanM = (3.5 * geom.spacing_mm + Math.abs(geom.right_mm)
                     + Math.abs(geom.fore_mm)) / 1000 + 5;
      const padLat = spanM / 111320;
      const padLon = spanM / (111320 *
        Math.cos(map.getCenter().lat * D) || 1);
      const s = b.getSouth() - padLat, n_ = b.getNorth() + padLat;
      const w = b.getWest() - padLon,  e = b.getEast() + padLon;
      if (visIdx.length < v.n) visIdx = new Int32Array(v.n * 2);
      let m = 0;
      for (let i = 0; i < v.n; i++) {
        const la = v.lat[i], lo = v.lon[i];
        if (la >= s && la <= n_ && lo >= w && lo <= e) visIdx[m++] = i;
      }
      return m;
    }

    /* Projection constants for the current view; container-pixel space. */
    function projSetup() {
      const scale = map.options.crs.scale(map.getZoom());
      const org = map.getPixelBounds().min;
      const cosC = Math.cos(map.getCenter().lat * D);
      return {
        scale, ox: org.x, oy: org.y,
        pxPerM: scale / (2 * Math.PI * 6378137 * cosC),
      };
    }
    function projX(P, lonDeg) { return P.scale * (lonDeg / 360 + 0.5) - P.ox; }
    function projY(P, latDeg) {
      const sin = Math.sin(latDeg * D);
      return P.scale * (0.5 - Math.log((1 + sin) / (1 - sin))
                              / (4 * Math.PI)) - P.oy;
    }

    function redraw() {
      dirty = false;
      const size = map.getSize();
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== size.x * dpr || canvas.height !== size.y * dpr) {
        canvas.width  = size.x * dpr; canvas.height = size.y * dpr;
        canvas.style.width = size.x + 'px'; canvas.style.height = size.y + 'px';
      }
      L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.x, size.y);
      if (!store || !store.length) return;

      const v = store.view();
      const P = projSetup();
      const nVis = cull(v);

      /* Track: antenna path, strided when huge (fidelity matters less than
       * a bounded path-build; canvas clips off-screen segments). */
      const stride = Math.max(1, Math.ceil(v.n / TRACK_MAX));
      ctx.beginPath();
      for (let i = 0, first = true; i < v.n; i += stride) {
        const x = projX(P, v.lon[i]), y = projY(P, v.lat[i]);
        if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = '#4444ff'; ctx.globalAlpha = 0.4;
      ctx.lineWidth = 1; ctx.stroke();
      ctx.globalAlpha = 0.75;

      const zero8 = calib.zero8, off = calib.offset;
      const minus = calib.minusRange, plus = calib.plusRange;
      /* Dot width = coil spacing on screen: adjacent coil tracks tile into
       * a seamless carpet at every zoom — touching, never overlapping. */
      const d = Math.max(1, (geom.spacing_mm / 1000) * P.pxPerM);
      const r = d / 2;
      const cmMode = filter === 'cm' && !!zero8;

      if (nVis > LOD_POINTS) {
        /* ── LOD: bin antenna positions, keep the strongest hit per cell ── */
        const gw = Math.ceil(size.x / LOD_CELL), gh = Math.ceil(size.y / LOD_CELL);
        const cells = gw * gh;
        if (cellIdx.length < cells) {
          cellIdx = new Int32Array(cells); cellDev = new Float32Array(cells);
        }
        cellIdx.fill(-1, 0, cells);
        cellDev.fill(-1, 0, cells);
        for (let k = 0; k < nVis; k++) {
          const i = visIdx[k];
          const cx = (projX(P, v.lon[i]) / LOD_CELL) | 0;
          const cy = (projY(P, v.lat[i]) / LOD_CELL) | 0;
          if (cx < 0 || cy < 0 || cx >= gw || cy >= gh) continue;
          const c = cy * gw + cx;
          let dev = 0;
          if (zero8) {
            const cm = cmMode ? cmOf(v, i, zero8) : 0;
            for (let ch = 0; ch < 8; ch++) {
              if (!visible[ch]) continue;
              const e = Math.abs(v.adc[i * 8 + ch] - cm - (zero8[ch] + off));
              if (e > dev) dev = e;
            }
          }
          if (dev >= cellDev[c]) { cellDev[c] = dev; cellIdx[c] = i; }
        }
        const s = Math.max(d, LOD_CELL);
        for (let c = 0; c < cells; c++) {
          const i = cellIdx[c];
          if (i < 0) continue;
          /* color by the winning channel's actual value (signed) */
          const cm = cmMode ? cmOf(v, i, zero8) : 0;
          let best = -1, bch = -1;
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            const e = zero8
              ? Math.abs(v.adc[i * 8 + ch] - cm - (zero8[ch] + off)) : 0;
            if (e >= best) { best = e; bch = ch; }
          }
          if (bch < 0) continue;                       // all channels hidden
          const x = projX(P, v.lon[i]), y = projY(P, v.lat[i]);
          ctx.fillStyle = zero8
            ? styleFor(v.adc[i * 8 + bch] - cm, zero8[bch] + off, minus, plus)
            : 'rgb(128,128,128)';
          ctx.fillRect(x - s / 2, y - s / 2, s, s);
        }
        return;
      }

      /* ── Full detail ── */
      const spacingM = geom.spacing_mm / 1000;
      const rightM0  = geom.right_mm / 1000, foreM = geom.fore_mm / 1000;
      const rot = geom.rot_deg || 0;

      if (combine) {
        /* Combine mode: value-domain accumulation per screen pixel.
         * Dots never paint over each other; their deviations COMBINE, so an
         * earlier pass can't be hidden (or washed out) by a later one. Within
         * one visit (points within REVISIT_GAP of each other) a cell keeps
         * its extreme value — along-track dot overlap adds nothing — but when
         * the array comes back over the same ground later, visits ADD: a
         * signal seen on two passes reads ~twice as strong. */
        const gw = size.x | 0, gh = size.y | 0, cells = gw * gh;
        if (accVal.length < cells) {
          accVal = new Float32Array(cells); accSeg = new Float32Array(cells);
          accLast = new Int32Array(cells);
        }
        accVal.fill(0, 0, cells); accSeg.fill(0, 0, cells);
        accLast.fill(-1, 0, cells);
        const dI = Math.max(1, Math.ceil(d));  // whole cells; ceil so rounding
                                               // can't open seams (overlap is
                                               // harmless: same-visit = max)
        for (let k = 0; k < nVis; k++) {
          const i = visIdx[k];
          const h = v.hdg[i];
          if (Number.isNaN(h)) continue;       // hollow dots drawn after blit
          const ax = projX(P, v.lon[i]), ay = projY(P, v.lat[i]);
          const a = (h + rot) * D;
          const fwdN = Math.cos(a), fwdE = Math.sin(a);
          const dN0 = foreM * fwdN, dE0 = foreM * fwdE;
          const cm = cmMode ? cmOf(v, i, zero8) : 0;
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            const rightM = rightM0 + (ch - 3.5) * spacingM;
            const dE = dE0 + rightM * fwdN;            // rgtE =  fwdN
            const dN = dN0 - rightM * fwdE;            // rgtN = -fwdE
            /* raw zeroed deviation only — the display offset is applied ONCE
             * at colorize, else a re-pass would double it into the tint */
            const dev = zero8 ? v.adc[i * 8 + ch] - cm - zero8[ch] : 0;
            const x0 = Math.max(0, Math.round(ax + dE * P.pxPerM - r));
            const y0 = Math.max(0, Math.round(ay - dN * P.pxPerM - r));
            const x1 = Math.min(gw, x0 + dI), y1 = Math.min(gh, y0 + dI);
            for (let cy = y0; cy < y1; cy++) {
              let c = cy * gw + x0;
              for (let cx = x0; cx < x1; cx++, c++) {
                if (i - accLast[c] > REVISIT_GAP) {  // new visit: settle old
                  accVal[c] += accSeg[c]; accSeg[c] = dev;
                } else if (Math.abs(dev) > Math.abs(accSeg[c])) {
                  accSeg[c] = dev;                   // same visit: extreme
                }
                accLast[c] = i;
              }
            }
          }
        }
        /* colorize the accumulated field in one blit (alpha where data) */
        if (!accImg || accImg.width !== gw || accImg.height !== gh) {
          accCanvas = document.createElement('canvas');
          accCanvas.width = gw; accCanvas.height = gh;
          accCtx = accCanvas.getContext('2d');
          accImg = accCtx.createImageData(gw, gh);
        }
        const px = accImg.data;
        const off1 = zero8 ? off : 0;    // uncalibrated stays neutral grey
        for (let c = 0; c < cells; c++) {
          const o = c * 4;
          if (accLast[c] < 0) { px[o + 3] = 0; continue; }
          const vv = accVal[c] + accSeg[c] - off1;
          let li;
          if (vv <= 0) {
            let t = minus > 0 ? (vv + minus) / minus : 0;
            if (t < 0) t = 0; else if (t > 1) t = 1;
            li = (t * 255) | 0;
          } else {
            let t = plus > 0 ? vv / plus : 0;
            if (t < 0) t = 0; else if (t > 1) t = 1;
            li = 256 + ((t * 255) | 0);
          }
          px[o]     = lutRGB[li * 3];
          px[o + 1] = lutRGB[li * 3 + 1];
          px[o + 2] = lutRGB[li * 3 + 2];
          px[o + 3] = 191;                            // 0.75 over the map
        }
        accCtx.putImageData(accImg, 0, 0);
        ctx.globalAlpha = 1;
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(accCanvas, 0, 0, gw, gh);
      } else {
        /* Classic paint: 8 coil dots per point, newest drawn last (covers
         * older overlapping passes — the un-combined comparison view). */
        for (let k = 0; k < nVis; k++) {
          const i = visIdx[k];
          const h = v.hdg[i];
          if (Number.isNaN(h)) continue;
          const ax = projX(P, v.lon[i]), ay = projY(P, v.lat[i]);
          const a = (h + rot) * D;
          const fwdN = Math.cos(a), fwdE = Math.sin(a);
          const dN0 = foreM * fwdN, dE0 = foreM * fwdE;
          const cm = cmMode ? cmOf(v, i, zero8) : 0;
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            const rightM = rightM0 + (ch - 3.5) * spacingM;
            const dE = dE0 + rightM * fwdN;            // rgtE =  fwdN
            const dN = dN0 - rightM * fwdE;            // rgtN = -fwdE
            const val = v.adc[i * 8 + ch];
            ctx.fillStyle = zero8
              ? styleFor(val - cm, zero8[ch] + off, minus, plus)
              : 'rgb(128,128,128)';
            ctx.fillRect(ax + dE * P.pxPerM - r, ay - dN * P.pxPerM - r, d, d);
          }
        }
      }
      /* No orientation: never guess coil positions — hollow antenna dots */
      ctx.globalAlpha = 0.75;
      ctx.strokeStyle = '#66aaff'; ctx.lineWidth = 1;
      for (let k = 0; k < nVis; k++) {
        const i = visIdx[k];
        if (!Number.isNaN(v.hdg[i])) continue;
        const ax = projX(P, v.lon[i]), ay = projY(P, v.lat[i]);
        ctx.strokeRect(ax - r - 1, ay - r - 1, d + 2, d + 2);
      }
    }

    /* Click → nearest dot within tolerance, full detail regardless of LOD
     * (a click is rare; one linear pass over in-view points is fine). */
    function hitTest(containerPoint) {
      if (!store || !store.length) return null;
      const v = store.view();
      const P = projSetup();
      const nVis = cull(v);
      /* tolerance tracks the zoom-dependent dot size (half-width + slack) */
      const dotPx = Math.max(1, (geom.spacing_mm / 1000) * P.pxPerM);
      const tol = Math.max(dotPx / 2 + 4, 8);
      const px = containerPoint.x, py = containerPoint.y;
      let best = tol * tol, hit = null;
      const spacingM = geom.spacing_mm / 1000;
      const rightM0  = geom.right_mm / 1000, foreM = geom.fore_mm / 1000;
      const rot = geom.rot_deg || 0;
      for (let k = 0; k < nVis; k++) {
        const i = visIdx[k];
        const h = v.hdg[i];
        const ax = projX(P, v.lon[i]), ay = projY(P, v.lat[i]);
        if (Number.isNaN(h)) {
          const dx = px - ax, dy = py - ay, d2 = dx * dx + dy * dy;
          if (d2 < best) { best = d2; hit = {i, ch: null}; }
          continue;
        }
        const a = (h + rot) * D;
        const fwdN = Math.cos(a), fwdE = Math.sin(a);
        const dN0 = foreM * fwdN, dE0 = foreM * fwdE;
        for (let ch = 0; ch < 8; ch++) {
          if (!visible[ch]) continue;
          const rightM = rightM0 + (ch - 3.5) * spacingM;
          const x = ax + (dE0 + rightM * fwdN) * P.pxPerM;
          const y = ay - (dN0 - rightM * fwdE) * P.pxPerM;
          const dx = px - x, dy = py - y, d2 = dx * dx + dy * dy;
          if (d2 < best) { best = d2; hit = {i, ch}; }
        }
      }
      if (!hit) return null;
      const i = hit.i;
      const out = {
        index: i, ch: hit.ch,
        heading: store.headingAt(i), fix: store.fixAt(i),
        adc: store.adcRow(i),
      };
      if (hit.ch == null) {
        out.latlng = [v.lat[i], v.lon[i]];
      } else {
        out.latlng = coilPositions(v.lat[i], v.lon[i], out.heading,
                                   geom)[hit.ch];
        out.val = v.adc[i * 8 + hit.ch];
        if (calib.zero8) {
          /* dev: the zeroed value that drives the color (pre-offset),
           * common-mode-corrected when the filter is on */
          const cm = filter === 'cm' ? cmOf(v, i, calib.zero8) : 0;
          out.dev = out.val - cm - calib.zero8[hit.ch];
        }
      }
      return out;
    }

    map.on('moveend zoomend viewreset resize', markDirty);

    return {
      setStore(s) { store = s; markDirty(); },
      setGeometry(g) { geom = g; markDirty(); },
      setCalib(c) { Object.assign(calib, c); markDirty(); },
      setFilter(f) { filter = f === 'cm' ? 'cm' : 'abs'; markDirty(); },
      setCombine(on) { combine = !!on; markDirty(); },
      setVisible(ch, on) { visible[ch] = on; markDirty(); },
      getVisible() { return visible.slice(); },
      markDirty,
      hitTest,
    };
  }

  return {coilPositions, geomFromMeta, colorFor, fixLabel, fixColor,
          reasonText, NavLayer, PointStore, ChannelRenderer,
          NAV_MIN_SPEED_MPS};
})();
