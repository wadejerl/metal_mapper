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
   *
   * Stamped values come from the board via the daemon: coil_spacing_mm and
   * coil_offset_right_mm (the ARRAY CENTRE's offset from the antenna, +
   * starboard). The view's Cal panel may override them per study, saved
   * with the view: sl_cal_pitch_mm, and sl_cal_ant_mm — the ANTENNA's
   * offset from the array centre, − port / + starboard, i.e. the negative
   * of right_mm (the operator's frame: where antenna 1 sits across the
   * array). Rotation: sl_cal_heading_deg if set, else sl_heading_offset_deg
   * (the unit's config.json, or a pre-move study's own saved value).
   * {stamped: true} ignores the Cal keys — the baseline the panel's reset
   * returns to and the alt-cal banner flag compares against. */
  function geomFromMeta(meta, opts) {
    const stamped = !!(opts && opts.stamped);
    const num = k => { const v = parseFloat(meta[k]); return Number.isFinite(v) ? v : null; };
    const cal = k => stamped ? null : num(k);
    const pitch = cal('sl_cal_pitch_mm'), ant = cal('sl_cal_ant_mm');
    const rot = cal('sl_cal_heading_deg');
    const right = num('coil_offset_right_mm'), rot0 = num('sl_heading_offset_deg');
    return {
      spacing_mm: pitch != null && pitch > 0 ? pitch : (num('coil_spacing_mm') || 330),
      fore_mm:    num('coil_offset_fore_mm') || 0,
      right_mm:   ant != null ? -ant : (right != null ? right : 500),
      rot_deg:    rot != null ? rot : (rot0 != null ? rot0 : 270),
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

  /* Nav-receiver lock status for the seeking HUD — one vocabulary for the
   * approach arrow, its badge and the map marker. Pins sit where an
   * RTK-FIXED trailer receiver said they were; a nav receiver that is
   * anything less puts the driver metres off the hole while the arrow
   * looks exactly the same, so the state has to live on the arrow.
   *   nav   = /navfix snapshot (null/undefined = never arrived)
   *   fresh = the page's transport gate (false = no tick lately; omit when
   *           the caller has no such gate)
   * → {code, level, label, color, ok}. ok only for a live RTK-fixed fix;
   * level 'warn' = RTK working but unconverged (wait), 'bad' = no RTK at
   * all / no fix / nothing live (check the corrections link). */
  function navFixStatus(nav, fresh) {
    const bad = (code, label) => ({code, level: 'bad', label, color: '#ff4444', ok: false});
    if (!nav || !nav.connected) return bad('down', 'NAV DOWN');
    if (fresh === false)        return bad('dead', 'NAV STALE');
    const q = nav.quality;
    if (nav.lat == null || q == null || q === 0) return bad('nofix', 'NO FIX');
    if (nav.stale)              return bad('stale', 'NAV STALE');
    if (q === 4) return {code: 'fixed', level: 'ok', label: 'RTK FIXED', color: fixColor(4), ok: true};
    if (q === 5) return {code: 'float', level: 'warn', label: 'RTK FLOAT', color: fixColor(5), ok: false};
    const name = (FIX_LABELS[q] || `FIX ${q}`).toUpperCase();
    return {code: 'nortk', level: 'bad', label: name + ' · NO RTK', color: fixColor(q), ok: false};
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
      const st = navFixStatus(nav);
      const color = nav.stale ? '#888' : st.color;   // shape: fix-colour legend
      // Anything short of RTK fixed wears a pulsing halo and says why, right
      // on the marker. Halo/tag speak the badge's language — red = no RTK /
      // no fix, amber = float, grey = stale — not the fix palette, which
      // shares one yellow between DGPS and RTK float. They sit in .nav-up,
      // which counter-rotates with the page's --unrot so the label reads
      // upright while the map turns in heading-up / target-up; the course
      // arrow stays outside it so it keeps pointing up (style.css).
      const sev = nav.stale ? '#888' : (st.level === 'bad' ? '#ff4444' : st.color);
      const warn = st.ok ? '' :
        `<div class="nav-up">` +
          `<div class="nav-halo nav-halo-${st.level}" style="border-color:${sev}"></div>` +
          `<div class="nav-tag" style="color:${sev}">${st.label}</div>` +
        `</div>`;
      if (moving) {
        return {
          mode: 'arrow',
          icon: L.divIcon({
            className: 'nav-icon',
            html: `<div class="nav-mk">${warn}<div style="transform: rotate(${nav.cog_deg}deg);
                     width:0;height:0;margin:2px auto;
                     border-left:9px solid transparent;
                     border-right:9px solid transparent;
                     border-bottom:22px solid ${color};
                     filter: drop-shadow(0 0 2px #000);"></div></div>`,
            iconSize: [26, 26], iconAnchor: [13, 13],
          }),
        };
      }
      return {
        mode: 'dot',
        icon: L.divIcon({
          className: 'nav-icon',
          html: `<div class="nav-mk">${warn}<div style="width:14px;height:14px;border-radius:50%;
                   background:${color};border:2px solid #000;
                   margin:5px auto;"></div></div>`,
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
   * heading NaN = none (drift test / --no-gate); fix 255 = none.
   * gt = the row's interpolated GPS timestamp in seconds (the firmware's
   * seconds-of-minute, wrapping at 60), NaN = none — the sync filter folds
   * on it, nothing else reads it. */
  /* ── /points wire format → typed-array views ─────────────────────────────
   * Server (db_map.py POINT_COLUMNS): 'MMP1', uint32 header length, JSON
   * header {n, last_id, cols: [[name, dtype, offset, count]]}, then the
   * columns, little-endian, each 8-byte aligned, offsets relative to the
   * data start. Views alias the fetched ArrayBuffer — no per-value parse,
   * which is the point: JSON.parse of a 100 MB body was its own memory
   * spike on the Pi's chromium. Throws on anything malformed. */
  const POINT_DTYPES = {u32: Uint32Array, f64: Float64Array, f32: Float32Array,
                        u8: Uint8Array, u16: Uint16Array};
  function decodePoints(buf) {
    const dv = new DataView(buf);
    if (buf.byteLength < 8 || dv.getUint32(0, true) !== 0x31504D4D)   // 'MMP1'
      throw new Error('points: bad magic');
    const hlen = dv.getUint32(4, true);
    if (8 + hlen > buf.byteLength) throw new Error('points: truncated header');
    const hdr = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, hlen)));
    const base = (8 + hlen + 7) & ~7;
    const out = {n: hdr.n, last_id: hdr.last_id, adc: new Array(8)};
    for (const [name, dtype, off, count] of hdr.cols) {
      const T = POINT_DTYPES[dtype];
      if (!T || count !== hdr.n) throw new Error('points: bad column ' + name);
      const at = base + off;
      if (at % T.BYTES_PER_ELEMENT || at + count * T.BYTES_PER_ELEMENT > buf.byteLength)
        throw new Error('points: column ' + name + ' out of range');
      const view = new T(buf, at, count);
      const m = /^adc([0-7])$/.exec(name);
      if (m) out.adc[+m[1]] = view; else out[name] = view;
    }
    for (const k of ['id', 'lat', 'lon', 'heading', 'fix', 'gps_ts'])
      if (!out[k]) throw new Error('points: missing column ' + k);
    for (let ch = 0; ch < 8; ch++)
      if (!out.adc[ch]) throw new Error('points: missing column adc' + ch);
    return out;
  }

  function PointStore() {
    let cap = 1024, n = 0;
    let id  = new Uint32Array(cap);
    let lat = new Float64Array(cap), lon = new Float64Array(cap);
    let hdg = new Float32Array(cap);
    let fix = new Uint8Array(cap);
    let adc = new Uint16Array(cap * 8);        // interleaved, 8 per point
    let gt  = new Float64Array(cap);

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
      gt  = grow(gt,  Float64Array, 1);
    }

    return {
      push(p) {                                // one SSE row dict
        ensure(n + 1);
        id[n]  = p.id; lat[n] = p.lat; lon[n] = p.lon;
        hdg[n] = (p.heading == null) ? NaN : p.heading;
        fix[n] = (p.fix == null) ? 255 : p.fix;
        // SSE rows carry gps_ts as the daemon's TEXT column; + parses or NaNs
        const g = p.gps_ts == null ? NaN : +p.gps_ts;
        gt[n] = Number.isFinite(g) ? g : NaN;
        for (let ch = 0; ch < 8; ch++) adc[n * 8 + ch] = p.adc[ch];
        n++;
      },
      fillBinary(cols) {                       // decodePoints() output
        const m = cols.n;
        if (!m) return;
        ensure(n + m);
        // Same dtypes and none-sentinels as push(), so bulk columns copy
        // straight in; only adc needs interleaving (8 columns → 8 per point).
        id.set(cols.id, n);  lat.set(cols.lat, n); lon.set(cols.lon, n);
        hdg.set(cols.heading, n); fix.set(cols.fix, n); gt.set(cols.gps_ts, n);
        for (let ch = 0; ch < 8; ch++) {
          const src = cols.adc[ch];
          for (let i = 0, j = n * 8 + ch; i < m; i++, j += 8) adc[j] = src[i];
        }
        n += m;
      },
      get length() { return n; },
      view() { return {n, id, lat, lon, hdg, fix, adc, gt}; },  // live refs: re-fetch after push
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
    let calib = {zero8: null, bias8: null,
                 offset: -140, plusRange: 450, minusRange: 450};

    /* Effective per-channel zeros: the calibrated zero plus the operator's
     * per-channel bias trim (balances coil-to-coil mismatch the single
     * global Offset can't). Trimming the ZERO (not the display offset)
     * makes the trim behave like calibration everywhere — including the
     * common-mode mean and combine-mode accumulation across re-passes. */
    function zEff() {
      const z = calib.zero8;
      if (!z) return null;
      const b = calib.bias8;
      if (!b) return z;
      const out = new Float64Array(8);
      for (let ch = 0; ch < 8; ch++) out[ch] = z[ch] + (b[ch] || 0);
      return out;
    }
    let geom  = {spacing_mm: 500, fore_mm: 0, right_mm: 0, rot_deg: 0};
    let useCm   = false; // subtract the array's common mode ('cm' filters)
    let useSync = false; // subtract the GPS-synchronous template ('sync')
    let cmSets  = true;  // common mode per 2×4 same-instant ADC set (see cmOf)
    /* Render mode — how overlapping coil samples resolve on screen:
     *  PAINT   classic: newest dots cover older ones (no accumulation)
     *  COMBINE per-visit extreme, and visits ADD (a re-pass reinforces)
     *  MEAN    average of every sample that landed on the cell, so noise
     *          cancels and ground that reads green on most visits stands
     *          out as a hot spot
     *  PEAK    the green-most (lowest) deviation the cell ever saw
     * Every mode but PAINT accumulates in the value domain per screen cell
     * — a pixel at full detail, a 4 px bin zoomed out (LOD). */
    const M_PAINT = 0, M_COMBINE = 1, M_MEAN = 2, M_PEAK = 3;
    const MODE_NAMES = ['paint', 'combine', 'mean', 'peak'];
    let mode = M_COMBINE;

    /* ── GPS-synchronous noise template ('sync' filter) ──────────────────
     * Field finding (2026-09): a broadband transient hits the front end
     * once per second, phase-locked to the GPS second (PPS-family source).
     * Fold every point on gps_ts mod period, take the per-channel MEDIAN
     * per phase bin (median so real targets — which land at random phases
     * while driving — can't bias it), re-center each channel's template to
     * zero mean (so subtracting it never shifts the calibrated zero), and
     * subtract at draw time. Points without gps_ts get no correction.
     * gps_ts wraps at 60 s, so only periods dividing 60 fold cleanly —
     * the UI offers 0.5/1/2 s, all sub/harmonics of the 1 Hz source. */
    const NOSYNC = 65535;            // bin sentinel: no gps_ts on this point
    let syncPeriod = 1.0, syncBins = 40;
    let syncTmpl = null;             // Float32Array(8 * syncBins), 0-mean/ch
    let syncBinIdx = new Uint16Array(0);   // phase bin per point
    let syncN = 0;                   // points binned so far
    let syncTmplN = 0;               // points in the current template
    const SYNC_REBUILD = 512;        // live: re-estimate every N new points

    function syncReset() { syncN = 0; syncTmplN = 0; syncTmpl = null; }

    /* Bin new points and (re)build the template when it is stale. Bins are
     * centered ON the fold's sample-phase grid (round, not floor): row
     * timestamps land exactly on 1/fs edges and float jitter must not
     * split one slot across two bins. */
    function ensureSync(v) {
      if (syncBinIdx.length < v.n) {
        const a = new Uint16Array(Math.max(v.n * 2, 1024));
        a.set(syncBinIdx.subarray(0, syncN));
        syncBinIdx = a;
      }
      for (let i = syncN; i < v.n; i++) {
        const g = v.gt[i];
        syncBinIdx[i] = Number.isNaN(g) ? NOSYNC
          : Math.round(((g % syncPeriod) + syncPeriod) % syncPeriod
                       / syncPeriod * syncBins) % syncBins;
      }
      syncN = v.n;
      if (syncTmpl && v.n - syncTmplN < SYNC_REBUILD) return;

      const B = syncBins;
      const counts = new Int32Array(B);
      for (let i = 0; i < v.n; i++) {
        if (syncBinIdx[i] !== NOSYNC) counts[syncBinIdx[i]]++;
      }
      const offs = new Int32Array(B + 1);
      for (let b = 0; b < B; b++) offs[b + 1] = offs[b] + counts[b];
      const total = offs[B];
      syncTmpl = new Float32Array(8 * B);
      syncTmplN = v.n;
      if (!total) return;
      const vals = new Float32Array(total);
      const cur = new Int32Array(B);
      for (let ch = 0; ch < 8; ch++) {
        cur.set(offs.subarray(0, B));
        for (let i = 0; i < v.n; i++) {
          const b = syncBinIdx[i];
          if (b !== NOSYNC) vals[cur[b]++] = v.adc[i * 8 + ch];
        }
        let mean = 0, nb = 0;
        for (let b = 0; b < B; b++) {
          const len = offs[b + 1] - offs[b];
          if (!len) continue;                  // empty bin: correction 0
          const s = vals.subarray(offs[b], offs[b + 1]);
          s.sort();                            // typed-array sort is numeric
          const med = len & 1 ? s[len >> 1] : (s[len / 2 - 1] + s[len / 2]) / 2;
          syncTmpl[ch * B + b] = med;
          mean += med; nb++;
        }
        if (!nb) continue;
        mean /= nb;
        for (let b = 0; b < B; b++) {
          if (offs[b + 1] > offs[b]) syncTmpl[ch * B + b] -= mean;
        }
      }
    }

    /* Per-channel sync correction at point i (0 when off / no gps_ts). */
    function scOf(sb, ch) {
      return (sb === NOSYNC || !syncTmpl) ? 0 : syncTmpl[ch * syncBins + sb];
    }

    /* Common mode at point i: mean zeroed deviation across ENABLED channels
     * (enabled-only so a dead/railed coil can be toggled out of the mean).
     * Subtracting it hides anything that moves all coils together — drift,
     * ground response — leaving only differential (target-like) signal.
     * sb: the point's sync bin (NOSYNC when the sync filter is off), so the
     * common mode is computed over sync-corrected values when both are on.
     *
     * The 8 channels are NOT sampled at one instant: the four ADCs each
     * convert an injected pair, so first-of-pair channels (even index) all
     * sample at one instant and second-of-pair (odd index) at the next.
     * A fast transient therefore lands with a different amplitude on the
     * two time-sets. cmSets=true averages each 2×4 set separately so the
     * mean is taken over same-instant samples; false keeps the classic
     * all-8 mean. Returns a 2-slot scratch indexed by (ch & 1). */
    const cmPair = new Float64Array(2);
    const CM_OFF = new Float64Array(2);      // constant zeros: cm disabled
    function cmOf(v, i, zero8, sb) {
      let s0 = 0, c0 = 0, s1 = 0, c1 = 0;
      for (let ch = 0; ch < 8; ch++) {
        if (!visible[ch]) continue;
        const d = v.adc[i * 8 + ch] - scOf(sb, ch) - zero8[ch];
        if (cmSets && (ch & 1)) { s1 += d; c1++; }
        else { s0 += d; c0++; }
      }
      if (cmSets) {
        cmPair[0] = c0 ? s0 / c0 : 0;
        cmPair[1] = c1 ? s1 / c1 : 0;
      } else {
        cmPair[0] = cmPair[1] = c0 ? s0 / c0 : 0;
      }
      return cmPair;
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
    let cellCh  = new Int8Array(0);      // LOD: its channel
    let accVal  = new Float32Array(0);   // cell: combine settled sum of past
                                         // visits / mean running sum / peak min
    let accSeg  = new Float32Array(0);   // combine: current visit's extreme
    let accCnt  = new Int32Array(0);     // mean: samples landed on the cell
    let accLast = new Int32Array(0);     // last point index per cell (-1 = empty)
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

    /* Value-domain accumulation shared by the LOD and full-detail paths in
     * every mode but PAINT. accReset sizes and clears the cell buffers;
     * accOut is a touched cell's display value — the display offset is
     * applied ONCE here, else a re-pass would double it into the tint. The
     * accumulation step itself is inlined at both call sites: it runs per
     * pixel per coil sample. */
    function accReset(cells) {
      if (accVal.length < cells) {
        accVal = new Float32Array(cells); accSeg = new Float32Array(cells);
        accCnt = new Int32Array(cells);   accLast = new Int32Array(cells);
      }
      accVal.fill(0, 0, cells); accSeg.fill(0, 0, cells);
      accCnt.fill(0, 0, cells); accLast.fill(-1, 0, cells);
    }
    function accOut(c, off1) {
      if (mode === M_COMBINE) return accVal[c] + accSeg[c] - off1;
      if (mode === M_MEAN)    return accVal[c] / accCnt[c] - off1;
      return accVal[c] - off1;                                    // PEAK
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

      const zero8 = zEff(), off = calib.offset;
      const minus = calib.minusRange, plus = calib.plusRange;
      /* Dot width = coil spacing on screen: adjacent coil tracks tile into
       * a seamless carpet at every zoom — touching, never overlapping. */
      const d = Math.max(1, (geom.spacing_mm / 1000) * P.pxPerM);
      const r = d / 2;
      const cmMode = useCm && !!zero8;
      const syncMode = useSync && !!zero8;
      if (syncMode) ensureSync(v);

      if (nVis > LOD_POINTS) {
        /* ── LOD: bin COIL positions per 4 px cell ──
         * Combine/Mean/Peak: the same value-domain accumulation as full
         * detail, at cell resolution — a re-pass still adds / averages /
         * keeps the green-most. Paint: keep the strongest hit per cell.
         * (LOD used to be strongest-per-cell in every mode, so a busy view
         * silently looked like Paint and flipped back when a small pan took
         * the in-view count under LOD_POINTS.)
         * Binning the coils (not the antenna point) preserves the array's
         * true ground footprint at every zoom: the swath stays a band that
         * touches/overlaps the neighboring pass, exactly as the coils did,
         * instead of collapsing into a one-cell-wide track line. */
        const gw = Math.ceil(size.x / LOD_CELL), gh = Math.ceil(size.y / LOD_CELL);
        const cells = gw * gh;
        if (cellIdx.length < cells) {
          cellIdx = new Int32Array(cells); cellDev = new Float32Array(cells);
          cellCh = new Int8Array(cells);
        }
        cellIdx.fill(-1, 0, cells);
        cellDev.fill(-1, 0, cells);
        if (mode !== M_PAINT) accReset(cells);
        const spacingML = geom.spacing_mm / 1000;
        const rightM0L  = geom.right_mm / 1000, foreML = geom.fore_mm / 1000;
        const rotL = geom.rot_deg || 0;
        for (let k = 0; k < nVis; k++) {
          const i = visIdx[k];
          const ax = projX(P, v.lon[i]), ay = projY(P, v.lat[i]);
          const sb = syncMode ? syncBinIdx[i] : NOSYNC;
          const cm = cmMode ? cmOf(v, i, zero8, sb) : CM_OFF;
          const h = v.hdg[i];
          const noHdg = Number.isNaN(h);   // no heading: all 8 at the antenna
          let fwdN = 0, fwdE = 0, dN0 = 0, dE0 = 0;
          if (!noHdg) {
            const a = (h + rotL) * D;
            fwdN = Math.cos(a); fwdE = Math.sin(a);
            dN0 = foreML * fwdN; dE0 = foreML * fwdE;
          }
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            let x = ax, y = ay;
            if (!noHdg) {
              const rightM = rightM0L + (ch - 3.5) * spacingML;
              const dE = dE0 + rightM * fwdN;          // rgtE =  fwdN
              const dN = dN0 - rightM * fwdE;          // rgtN = -fwdE
              x = ax + dE * P.pxPerM; y = ay - dN * P.pxPerM;
            }
            const cx = (x / LOD_CELL) | 0, cy = (y / LOD_CELL) | 0;
            if (cx < 0 || cy < 0 || cx >= gw || cy >= gh) continue;
            const c = cy * gw + cx;
            if (mode !== M_PAINT) {
              /* raw zeroed deviation only — display offset applied once at
               * colorize (accOut), else a re-pass would double it */
              const dev = zero8
                ? v.adc[i * 8 + ch] - scOf(sb, ch) - cm[ch & 1] - zero8[ch] : 0;
              if (mode === M_COMBINE) {
                if (i - accLast[c] > REVISIT_GAP) {  // new visit: settle old
                  accVal[c] += accSeg[c]; accSeg[c] = dev;
                } else if (Math.abs(dev) > Math.abs(accSeg[c])) {
                  accSeg[c] = dev;                   // same visit: extreme
                }
              } else if (mode === M_MEAN) {
                accVal[c] += dev; accCnt[c]++;
              } else if (accLast[c] < 0 || dev < accVal[c]) {
                accVal[c] = dev;                     // PEAK: green-most wins
              }
              accLast[c] = i;
              continue;
            }
            const e = zero8
              ? Math.abs(v.adc[i * 8 + ch] - scOf(sb, ch) - cm[ch & 1]
                         - (zero8[ch] + off)) : 0;
            if (e >= cellDev[c]) { cellDev[c] = e; cellIdx[c] = i; cellCh[c] = ch; }
          }
        }
        /* Paint at the cell, sized so covered cells tile into a carpet. */
        const s = Math.max(d, LOD_CELL);
        if (mode !== M_PAINT) {
          const off1 = zero8 ? off : 0;    // uncalibrated stays neutral grey
          for (let c = 0; c < cells; c++) {
            if (accLast[c] < 0) continue;
            const x = (c % gw) * LOD_CELL + LOD_CELL / 2;
            const y = ((c / gw) | 0) * LOD_CELL + LOD_CELL / 2;
            ctx.fillStyle = zero8
              ? styleFor(accOut(c, off1), 0, minus, plus)
              : 'rgb(128,128,128)';
            ctx.fillRect(x - s / 2, y - s / 2, s, s);
          }
          return;
        }
        for (let c = 0; c < cells; c++) {
          const i = cellIdx[c];
          if (i < 0) continue;
          const ch = cellCh[c];
          const sb = syncMode ? syncBinIdx[i] : NOSYNC;
          const cm = cmMode ? cmOf(v, i, zero8, sb) : CM_OFF;
          const x = (c % gw) * LOD_CELL + LOD_CELL / 2;
          const y = ((c / gw) | 0) * LOD_CELL + LOD_CELL / 2;
          ctx.fillStyle = zero8
            ? styleFor(v.adc[i * 8 + ch] - scOf(sb, ch) - cm[ch & 1],
                       zero8[ch] + off, minus, plus)
            : 'rgb(128,128,128)';
          ctx.fillRect(x - s / 2, y - s / 2, s, s);
        }
        return;
      }

      /* ── Full detail ── */
      const spacingM = geom.spacing_mm / 1000;
      const rightM0  = geom.right_mm / 1000, foreM = geom.fore_mm / 1000;
      const rot = geom.rot_deg || 0;

      if (mode !== M_PAINT) {
        /* Value-domain accumulation per screen pixel. Dots never paint over
         * each other; their deviations are resolved per mode:
         * COMBINE — within one visit (points within REVISIT_GAP of each
         *   other) a cell keeps its extreme value, so along-track dot
         *   overlap adds nothing, but when the array comes back over the
         *   same ground later the visits ADD: a signal seen on two passes
         *   reads ~twice as strong and an earlier pass is never hidden.
         * MEAN — every coil sample that lands on the pixel is averaged
         *   (along-track overlap included), so uncorrelated noise cancels
         *   and ground that is green on most visits stays green.
         * PEAK — the pixel keeps the lowest (green-most) deviation seen. */
        const gw = size.x | 0, gh = size.y | 0, cells = gw * gh;
        accReset(cells);
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
          const sb = syncMode ? syncBinIdx[i] : NOSYNC;
          const cm = cmMode ? cmOf(v, i, zero8, sb) : CM_OFF;
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            const rightM = rightM0 + (ch - 3.5) * spacingM;
            const dE = dE0 + rightM * fwdN;            // rgtE =  fwdN
            const dN = dN0 - rightM * fwdE;            // rgtN = -fwdE
            /* raw zeroed deviation only — the display offset is applied ONCE
             * at colorize, else a re-pass would double it into the tint */
            const dev = zero8
              ? v.adc[i * 8 + ch] - scOf(sb, ch) - cm[ch & 1] - zero8[ch] : 0;
            const x0 = Math.max(0, Math.round(ax + dE * P.pxPerM - r));
            const y0 = Math.max(0, Math.round(ay - dN * P.pxPerM - r));
            const x1 = Math.min(gw, x0 + dI), y1 = Math.min(gh, y0 + dI);
            for (let cy = y0; cy < y1; cy++) {
              let c = cy * gw + x0;
              for (let cx = x0; cx < x1; cx++, c++) {
                if (mode === M_COMBINE) {
                  if (i - accLast[c] > REVISIT_GAP) {  // new visit: settle
                    accVal[c] += accSeg[c]; accSeg[c] = dev;
                  } else if (Math.abs(dev) > Math.abs(accSeg[c])) {
                    accSeg[c] = dev;                   // same visit: extreme
                  }
                } else if (mode === M_MEAN) {
                  accVal[c] += dev; accCnt[c]++;
                } else if (accLast[c] < 0 || dev < accVal[c]) {
                  accVal[c] = dev;                     // PEAK: green-most
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
          const vv = accOut(c, off1);
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
        /* PAINT: 8 coil dots per point, newest drawn last (covers older
         * overlapping passes — the classic comparison view). */
        for (let k = 0; k < nVis; k++) {
          const i = visIdx[k];
          const h = v.hdg[i];
          if (Number.isNaN(h)) continue;
          const ax = projX(P, v.lon[i]), ay = projY(P, v.lat[i]);
          const a = (h + rot) * D;
          const fwdN = Math.cos(a), fwdE = Math.sin(a);
          const dN0 = foreM * fwdN, dE0 = foreM * fwdE;
          const sb = syncMode ? syncBinIdx[i] : NOSYNC;
          const cm = cmMode ? cmOf(v, i, zero8, sb) : CM_OFF;
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            const rightM = rightM0 + (ch - 3.5) * spacingM;
            const dE = dE0 + rightM * fwdN;            // rgtE =  fwdN
            const dN = dN0 - rightM * fwdE;            // rgtN = -fwdE
            const val = v.adc[i * 8 + ch] - scOf(sb, ch);
            ctx.fillStyle = zero8
              ? styleFor(val - cm[ch & 1], zero8[ch] + off, minus, plus)
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
        const z8 = zEff();
        if (z8) {
          /* dev: the zeroed value that drives the color (pre-offset),
           * filter-corrected the same way the dots are */
          let sb = NOSYNC;
          if (useSync) { ensureSync(v); sb = syncBinIdx[i]; }
          const cm = useCm ? cmOf(v, i, z8, sb)[hit.ch & 1] : 0;
          const sc = scOf(sb, hit.ch);
          out.dev = out.val - sc - cm - z8[hit.ch];
          if (sc) out.sync = sc;   // popup can show what the filter removed
        }
      }
      return out;
    }

    /* ── Green census: where the map is green, on a metre grid ────────────
     * Field ask (2026-09, first live use): rank dig pins by how much green
     * sits under them, and propose new pins where the most green is. Green
     * is exactly what colorFor paints green — filter-corrected zeroed
     * deviation <= Offset, per coil sample — so the ranking follows the
     * sliders the operator tuned, not a second definition of a hit. Every
     * coil sample of every point (not just in view) is binned into a
     * sparse cell grid in local metres about the study's SW corner; sizes
     * are then window counts over that grid (see MM.greenCount/greenPeaks).
     * Sorted cell ids + binary search: ~4 B per green sample, no hash map,
     * and a 950k-point study scans in about a second. A point without
     * heading is skipped: at pin-placing zoom the full-detail paint draws
     * it as an uncoloured antenna outline, never green (only the zoomed-out
     * LOD path piles its 8 coils on the antenna), so counting it would
     * rank spots the operator cannot see. Returns null when nothing is
     * calibrated yet. */
    function greenIndex(cellM) {
      const zero8 = zEff();
      if (!store || !store.length || !zero8) return null;
      const v = store.view();
      const cell = (cellM > 0 && Number.isFinite(cellM)) ? cellM : 0.1;
      const off = calib.offset;
      const cmMode = useCm, syncMode = useSync;
      if (syncMode) ensureSync(v);
      let laMin = Infinity, laMax = -Infinity, loMin = Infinity, loMax = -Infinity;
      for (let i = 0; i < v.n; i++) {
        const la = v.lat[i], lo = v.lon[i];
        if (la < laMin) laMin = la; if (la > laMax) laMax = la;
        if (lo < loMin) loMin = lo; if (lo > loMax) loMax = lo;
      }
      const spanM = (3.5 * geom.spacing_mm + Math.abs(geom.right_mm)
                     + Math.abs(geom.fore_mm)) / 1000 + 2;
      const mLon = M_PER_DEG_LAT * Math.cos((laMin + laMax) / 2 * D) || 1;
      const lat0 = laMin - spanM / M_PER_DEG_LAT;
      const lon0 = loMin - spanM / mLon;
      const W = Math.ceil(((loMax - lon0) * mLon + spanM) / cell) + 1;
      const H = Math.ceil(((laMax - lat0) * M_PER_DEG_LAT + spanM) / cell) + 1;
      if (!(W > 0 && H > 0) || W * H > 2000000000) return null;
      const spacingM = geom.spacing_mm / 1000;
      const rightM0  = geom.right_mm / 1000, foreM = geom.fore_mm / 1000;
      const rot = geom.rot_deg || 0;
      /* pass 0 counts the green samples, pass 1 fills their cell ids */
      let ids = null, g = 0;
      for (let pass = 0; pass < 2; pass++) {
        if (pass) { ids = new Int32Array(g); g = 0; }
        for (let i = 0; i < v.n; i++) {
          const h = v.hdg[i];
          if (Number.isNaN(h)) continue;   // no heading: no coil positions
          const sb = syncMode ? syncBinIdx[i] : NOSYNC;
          const cm = cmMode ? cmOf(v, i, zero8, sb) : CM_OFF;
          const a = (h + rot) * D;
          const fwdN = Math.cos(a), fwdE = Math.sin(a);
          const ax = (v.lon[i] - lon0) * mLon + foreM * fwdE;   // metres E
          const ay = (v.lat[i] - lat0) * M_PER_DEG_LAT + foreM * fwdN; // N
          for (let ch = 0; ch < 8; ch++) {
            if (!visible[ch]) continue;
            const dev = v.adc[i * 8 + ch] - scOf(sb, ch) - cm[ch & 1] - zero8[ch];
            if (dev > off) continue;                 // not green
            if (pass) {
              const rightM = rightM0 + (ch - 3.5) * spacingM;
              const ix = Math.floor((ax + rightM * fwdN) / cell);  // rgtE = fwdN
              const iy = Math.floor((ay - rightM * fwdE) / cell);  // rgtN = -fwdE
              if (ix < 0 || iy < 0 || ix >= W || iy >= H) { ids[g] = -1; g++; continue; }
              ids[g] = iy * W + ix;
            }
            g++;
          }
        }
      }
      ids.sort();                                    // typed array: numeric
      let k0 = 0;
      while (k0 < g && ids[k0] < 0) k0++;            // out-of-grid (never, in practice)
      let u = 0;
      for (let k = k0; k < g; k++) if (k === k0 || ids[k] !== ids[k - 1]) u++;
      const uid = new Int32Array(u), cnt = new Uint32Array(u);
      for (let k = k0, j = -1; k < g; k++) {
        if (k === k0 || ids[k] !== ids[k - 1]) { uid[++j] = ids[k]; cnt[j] = 1; }
        else cnt[j]++;
      }
      return {cell, W, H, lat0, lon0, mLon, ids: uid, cnt, green: g - k0,
              points: v.n, offset: off,
              filter: (cmMode ? 'cm' : 'abs') + (syncMode ? '+sync' : '')};
    }

    map.on('moveend zoomend viewreset resize', markDirty);

    return {
      setStore(s) { store = s; syncReset(); markDirty(); },
      setGeometry(g) { geom = g; markDirty(); },
      setCalib(c) { Object.assign(calib, c); markDirty(); },
      /* 'abs' | 'cm' | 'sync' | 'cm_sync' — cm and sync are independent
       * corrections and compose (sync first, then common mode). */
      setFilter(f) {
        useCm   = f === 'cm' || f === 'cm_sync';
        useSync = f === 'sync' || f === 'cm_sync';
        markDirty();
      },
      setSyncCfg(cfg) {
        const p = +cfg.period, b = Math.round(+cfg.bins);
        const period = (p > 0 && Number.isFinite(p)) ? p : 1.0;
        const bins = Math.min(400, Math.max(8, Number.isFinite(b) ? b : 40));
        if (period === syncPeriod && bins === syncBins) return;
        syncPeriod = period; syncBins = bins;
        syncReset();                 // rebin + re-estimate on next redraw
        markDirty();
      },
      /* true: common mode per 2×4 same-instant ADC set; false: all-8 mean */
      setCmSets(on) {
        on = !!on;
        if (on === cmSets) return;
        cmSets = on; markDirty();
      },
      /* 'paint' | 'combine' | 'mean' | 'peak' (see the M_* notes); an
       * unknown name is ignored and reported false */
      setMode(name) {
        const m = MODE_NAMES.indexOf(String(name).toLowerCase());
        if (m < 0) return false;
        if (m !== mode) { mode = m; markDirty(); }
        return true;
      },
      getMode() { return MODE_NAMES[mode]; },
      setCombine(on) { this.setMode(on ? 'combine' : 'paint'); },
      setVisible(ch, on) { visible[ch] = on; markDirty(); },
      getVisible() { return visible.slice(); },
      markDirty,
      hitTest,
      greenIndex,
    };
  }

  /* ── Green-census queries (pure functions over a greenIndex) ──────────────
   * size(pin) = number of green coil samples whose grid cell centre lies
   * within r metres of the pin (the cell under the pin always counts, so a
   * radius smaller than a cell still reads the ground under the pin). Two
   * passes over the same spot add up, as they do in Combine mode. */
  function _findCell(ids, id) {
    let lo = 0, hi = ids.length - 1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1, t = ids[m];
      if (t === id) return m;
      if (t < id) lo = m + 1; else hi = m - 1;
    }
    return -1;
  }
  /* window count + count-weighted centroid about local (x, y) metres */
  function _windowStat(idx, x, y, r) {
    const c = idx.cell, r2 = r * r;
    const cx = Math.floor(x / c), cy = Math.floor(y / c);
    const ix0 = Math.max(0, Math.floor((x - r) / c));
    const ix1 = Math.min(idx.W - 1, Math.floor((x + r) / c));
    const iy0 = Math.max(0, Math.floor((y - r) / c));
    const iy1 = Math.min(idx.H - 1, Math.floor((y + r) / c));
    let s = 0, sx = 0, sy = 0;
    for (let iy = iy0; iy <= iy1; iy++) {
      const yc = (iy + 0.5) * c, dy = yc - y;
      for (let ix = ix0; ix <= ix1; ix++) {
        const xc = (ix + 0.5) * c, dx = xc - x;
        if (dx * dx + dy * dy > r2 && !(ix === cx && iy === cy)) continue;
        const k = _findCell(idx.ids, iy * idx.W + ix);
        if (k < 0) continue;
        const n = idx.cnt[k];
        s += n; sx += n * xc; sy += n * yc;
      }
    }
    return s ? {s, x: sx / s, y: sy / s} : {s: 0, x, y};
  }
  function _toLocal(idx, lat, lon) {
    return [(lon - idx.lon0) * idx.mLon, (lat - idx.lat0) * M_PER_DEG_LAT];
  }
  function greenCount(idx, lat, lon, rM) {
    if (!idx) return 0;
    const [x, y] = _toLocal(idx, lat, lon);
    return _windowStat(idx, x, y, rM).s;
  }
  /* The n biggest green spots not already marked: score every occupied
   * cell by its window count, walk them largest-first, and keep a spot
   * unless it is within minSep of an existing pin or an earlier pick (one
   * target spreads over many cells — the first cell wins, the rest of the
   * blob is suppressed). Each pick is refined to the count-weighted
   * centroid of its window, so it lands on the middle of the green, not
   * on a cell corner. Returned largest-first, so ids minted from this
   * list ARE the stack rank. */
  function greenPeaks(idx, rM, opts) {
    opts = opts || {};
    const n = Math.max(1, opts.n | 0 || 5);
    const minSep = opts.minSep > 0 ? opts.minSep : Math.max(1.0, 2 * rM);
    const minSize = opts.minSize > 0 ? opts.minSize : 1;
    const excl = (opts.exclude || []).map(p => _toLocal(idx, p.lat, p.lon));
    const U = idx.ids.length;
    if (!U) return [];
    const PACK = 16777216;                     // 2^24 > any cell count here
    const score = new Float64Array(U);
    for (let k = 0; k < U; k++) {
      const id = idx.ids[k], ix = id % idx.W, iy = (id / idx.W) | 0;
      const st = _windowStat(idx, (ix + 0.5) * idx.cell, (iy + 0.5) * idx.cell, rM);
      score[k] = Math.min(st.s, PACK - 1) * PACK + k;
    }
    score.sort();
    const picks = [], sep2 = minSep * minSep;
    for (let q = U - 1; q >= 0 && picks.length < n; q--) {
      const packed = score[q];
      const s = Math.floor(packed / PACK), k = packed - s * PACK;
      if (s < minSize) break;
      const id = idx.ids[k], ix = id % idx.W, iy = (id / idx.W) | 0;
      const cx = (ix + 0.5) * idx.cell, cy = (iy + 0.5) * idx.cell;
      let near = false;
      for (const p of excl) {
        const dx = p[0] - cx, dy = p[1] - cy;
        if (dx * dx + dy * dy < sep2) { near = true; break; }
      }
      if (!near) for (const p of picks) {
        const dx = p.x - cx, dy = p.y - cy;
        if (dx * dx + dy * dy < sep2) { near = true; break; }
      }
      if (near) continue;
      /* refine to the centroid, then re-check separation THERE: two cells
       * on the flanks of one blob both refine toward its middle and would
       * otherwise pass the cell-centre test yet land on top of each other */
      const st = _windowStat(idx, cx, cy, rM);
      for (const p of picks) {
        const dx = p.x - st.x, dy = p.y - st.y;
        if (dx * dx + dy * dy < sep2) { near = true; break; }
      }
      if (!near) for (const p of excl) {
        const dx = p[0] - st.x, dy = p[1] - st.y;
        if (dx * dx + dy * dy < sep2) { near = true; break; }
      }
      if (near) continue;
      /* size as a pin dropped here will later measure it (greenCount at
       * the refined spot), so Rank agrees with Find */
      const size = _windowStat(idx, st.x, st.y, rM).s;
      picks.push({x: st.x, y: st.y, size,
                  lat: idx.lat0 + st.y / M_PER_DEG_LAT,
                  lon: idx.lon0 + st.x / idx.mLon});
    }
    // the walk is ordered by cell-centre score; the refined sizes can
    // reorder neighbours, and the caller mints ids from THIS order
    picks.sort((a, b) => b.size - a.size);
    return picks;
  }

  /* ── Channel strip chart (shared by the map view and the raw view) ────────
   * Scrolling multi-channel trace: newest sample at the right edge, one line
   * per channel in CH_COLORS, ONE shared Y scale so the traces overlay.
   * Fed from the daemon's live_samples stream (full sample rate), so it
   * works with no GPS fix and no recording — it is a scope, not a log.
   *
   * Scale modes:
   *   'auto' — RAW counts, Y bounds fit the visible data. ONE mapping
   *            (gain AND offset) for all channels, so both levels and
   *            amplitudes are directly comparable — channels sitting at
   *            different DC points fan into separate traces, as they must.
   *   'zero' — each channel plotted as DEVIATION from its zero baseline
   *            (per-channel offset removal overlays the traces), shared
   *            gain fit to the visible deviations. Dashed zero line.
   *            Levels are NOT comparable here — only response shapes.
   *   'abs'  — RAW counts on a fixed 0..65535 axis: where each analog
   *            stage actually sits, rails included. Zeros/filter ignored.
   * filter 'cm' (auto/zero modes) subtracts the array-mean deviation per
   * sample — same Δ-array-mean as the map coloring. */
  const CH_COLORS = ['#e6194b', '#f58231', '#ffe119', '#3cb44b',
                     '#42d4f4', '#4363d8', '#911eb4', '#f032e6'];

  function StripChart(canvas, opts) {
    opts = opts || {};
    let W = opts.width  || 220;
    let H = opts.height || 88;
    let N = opts.n      || 240;         // samples kept
    const buf = [];                     // rows of adc[8], newest last
    let zero8 = null;
    let scaleMode = 'auto';             // 'auto' | 'zero' | 'abs'
    let filter = 'abs';                 // 'abs' | 'cm' (Δ array mean)
    const vis = [true, true, true, true, true, true, true, true];
    const ctx = canvas.getContext('2d');

    function size() {
      const dpr = window.devicePixelRatio || 1;
      canvas.width  = W * dpr;
      canvas.height = H * dpr;
      canvas.style.width  = W + 'px';
      canvas.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    size();

    function seriesOf(row) {
      if (scaleMode === 'abs') return row;
      // deviations drive the cm filter in both modes; only 'zero' plots them
      const dev = zero8 ? row.map((v, ch) => v - zero8[ch]) : row.slice();
      const base = scaleMode === 'zero' ? dev : row;
      if (filter === 'cm') {
        const m = dev.reduce((a, b) => a + b, 0) / dev.length;
        return base.map(v => v - m);
      }
      return base;
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      const n = buf.length;
      if (n < 2) return;
      const rows = buf.map(seriesOf);
      let top, bot;
      if (scaleMode === 'abs') {
        top = 65535; bot = 0;
      } else {
        // Fit VISIBLE channels only: unticking a railed channel is how the
        // operator zooms the shared axis into the quiet ones.
        let lo = Infinity, hi = -Infinity;
        for (const row of rows)
          for (let ch = 0; ch < 8; ch++) {
            if (!vis[ch]) continue;
            const v = row[ch];
            if (v < lo) lo = v; if (v > hi) hi = v;
          }
        if (lo > hi) { lo = 0; hi = 1; }  // everything hidden: keep a frame
        if (hi - lo < 1) { hi += 1; lo -= 1; }
        const pad = (hi - lo) * 0.06;
        top = hi + pad; bot = lo - pad;
      }
      const step = W / (N - 1);
      const x0   = W - (n - 1) * step;  // newest anchored at the right edge
      // Inset the trace band: a value AT a scale bound (rails in 'abs' mode)
      // otherwise strokes centered on the canvas edge and shows only as a
      // half-clipped smear. auto/zero pad their fitted range, but the fixed
      // 0..65535 axis has values exactly at its bounds. The half-pixel puts
      // a bound-value stroke on a pixel center: crisp, not two dim rows.
      const EDGE = 1.5;
      const yOf  = v => EDGE + (top - v) / (top - bot) * (H - 2 * EDGE);
      if (scaleMode === 'zero' && zero8 && top > 0 && bot < 0) {
        ctx.strokeStyle = 'rgba(255,255,255,0.25)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(0, yOf(0)); ctx.lineTo(W, yOf(0));
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.lineWidth = 1;
      for (let ch = 0; ch < 8; ch++) {
        if (!vis[ch]) continue;
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
          const x = x0 + i * step, y = yOf(rows[i][ch]);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = CH_COLORS[ch];
        ctx.stroke();
      }
      // shared-scale bounds: raw counts in auto/abs, Δ counts in zero mode
      ctx.fillStyle = '#999';
      ctx.font = '9px monospace';
      ctx.fillText(Math.round(top), 2, 9);
      ctx.fillText(Math.round(bot), 2, H - 2);
    }

    return {
      append(adc) {
        if (!adc) return;
        buf.push(Array.from(adc));
        if (buf.length > N) buf.splice(0, buf.length - N);
      },
      draw,
      clear() { buf.length = 0; draw(); },
      tail(k) { return buf.slice(-k); },
      get length() { return buf.length; },
      setZero(z) { zero8 = z; },
      setScaleMode(m) {
        scaleMode = (m === 'abs' || m === 'zero') ? m : 'auto';
      },
      setFilter(f) { filter = f === 'cm' ? 'cm' : 'abs'; },
      setChanVis(ch, on) { if (ch >= 0 && ch < 8) vis[ch] = !!on; },
      chanVis(ch) { return vis[ch]; },
      // Per-channel mean/σ over the buffer, run through the SAME seriesOf
      // pipeline the traces are drawn with — so with the Δ-array-mean
      // filter active the σ is differential noise, matching what the eye
      // sees: a fat trace and a big number always agree. Hidden channels
      // are still computed (their legend row keeps reading).
      stats() {
        const n = buf.length;
        if (n < 2) return null;
        const sum = new Array(8).fill(0), sq = new Array(8).fill(0);
        for (const row of buf) {
          const s = seriesOf(row);
          for (let ch = 0; ch < 8; ch++) {
            sum[ch] += s[ch];
            sq[ch]  += s[ch] * s[ch];
          }
        }
        return sum.map((s, ch) => {
          const mean = s / n;
          const varc = Math.max(0, sq[ch] / n - mean * mean);
          return {mean, std: Math.sqrt(varc)};
        });
      },
      resize(w, h, n) {
        W = w; H = h;
        if (n) { N = n; if (buf.length > N) buf.splice(0, buf.length - N); }
        size();
      },
    };
  }

  return {coilPositions, geomFromMeta, colorFor, fixLabel, fixColor, navFixStatus,
          reasonText, NavLayer, PointStore, ChannelRenderer, StripChart,
          decodePoints, greenCount, greenPeaks, CH_COLORS, NAV_MIN_SPEED_MPS};
})();
