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

  /* ── 8-channel dot renderer (one canvas, one LayerGroup per channel) ────── */

  function ChannelRenderer(map) {
    const canvas = L.canvas({padding: 0.3});
    const groups = [];             // LayerGroup per channel
    const visible = new Array(8).fill(true);
    for (let i = 0; i < 8; i++) {
      groups.push(L.layerGroup().addTo(map));
    }
    const noHdg = L.layerGroup().addTo(map);  // heading-less antenna dots
    let track = null;              // antenna-path polyline
    let calib = {zero8: null, offset: 0, plusRange: 1600, minusRange: 1600,
                 dotSize: 2};
    let geom = {spacing_mm: 500, fore_mm: 0, right_mm: 0, rot_deg: 0};
    let count = 0;

    /* Only the newest ~100 points keep popups. Live appends run for hours
     * at ~20 points/s — binding a popup to every marker forever is the
     * unbounded-growth path, and it also matches renderAll's last-100 cap. */
    const POPUP_KEEP = 800;        // 100 points x 8 channels
    const popupQueue = [];
    function rememberPopup(m) {
      popupQueue.push(m);
      while (popupQueue.length > POPUP_KEEP) popupQueue.shift().unbindPopup();
    }

    function dotStyle(ch, val) {
      const zero = calib.zero8 ? calib.zero8[ch] : null;
      const c = colorFor(val, zero, calib.offset,
                         calib.plusRange, calib.minusRange);
      return {renderer: canvas, radius: calib.dotSize, fillColor: c,
              color: c, weight: 0.3, opacity: 1, fillOpacity: 0.75};
    }

    function addPoint(p, withPopup, extendTrack = true) {
      // extendTrack=false during renderAll: the polyline is built there in
      // one shot — per-point addLatLng() on a mapped polyline redraws the
      // whole path each call, which is O(N^2) over a real study.
      if (extendTrack && track) track.addLatLng([p.lat, p.lon]);
      if (p.heading == null) {
        // No orientation (stationary drift test / --no-gate): never guess
        // coil positions — mark the antenna position itself instead.
        const m = L.circleMarker([p.lat, p.lon],
          {renderer: canvas, radius: calib.dotSize + 1, fillColor: '#3388ff',
           color: '#66aaff', weight: 1, opacity: 0.9, fillOpacity: 0.2});
        if (withPopup) {
          m.bindPopup(`antenna · no heading — coils not drawn<br>` +
                      `${p.lat.toFixed(7)}, ${p.lon.toFixed(7)}<br>` +
                      `fix ${fixLabel(p.fix)}`);
          rememberPopup(m);
        }
        noHdg.addLayer(m);
        count++;
        return;
      }
      const pos = coilPositions(p.lat, p.lon, p.heading, geom);
      for (let ch = 0; ch < 8; ch++) {
        const m = L.circleMarker(pos[ch], dotStyle(ch, p.adc[ch]));
        if (withPopup) {
          m.bindPopup(`ch${ch} · adc ${p.adc[ch]}<br>` +
                      `${pos[ch][0].toFixed(7)}, ${pos[ch][1].toFixed(7)}<br>` +
                      `heading ${p.heading.toFixed(1)}°  fix ${fixLabel(p.fix)}`);
          rememberPopup(m);
        }
        groups[ch].addLayer(m);
      }
      count++;
    }

    return {
      setGeometry(g) { geom = g; },
      setCalib(c) { Object.assign(calib, c); },
      setVisible(ch, on) {
        visible[ch] = on;
        if (on) map.addLayer(groups[ch]); else map.removeLayer(groups[ch]);
      },
      getVisible() { return visible.slice(); },
      addPoint,
      renderAll(points) {
        groups.forEach(g => g.clearLayers());
        noHdg.clearLayers();
        popupQueue.length = 0;     // markers just discarded — drop the refs
        if (track) { map.removeLayer(track); track = null; }
        count = 0;
        if (!points.length) return;
        track = L.polyline(points.map(p => [p.lat, p.lon]),
                           {renderer: canvas, color: '#4444ff',
                            weight: 1, opacity: 0.4}).addTo(map);
        const popupFrom = Math.max(0, points.length - 100);
        points.forEach((p, i) => addPoint(p, i >= popupFrom, false));
      },
      appendLive(p) {
        if (!track) {
          track = L.polyline([], {renderer: canvas, color: '#4444ff',
                                  weight: 1, opacity: 0.4}).addTo(map);
        }
        addPoint(p, true);
      },
      count() { return count; },
    };
  }

  return {coilPositions, geomFromMeta, colorFor, fixLabel, fixColor,
          reasonText, NavLayer, ChannelRenderer, NAV_MIN_SPEED_MPS};
})();
