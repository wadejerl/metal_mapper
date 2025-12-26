#!/usr/bin/env python3
"""
Real-time serial data mapper using Folium and Flask.
Reads lat,lon,value from serial port and plots them on an interactive map.
"""
import sys
import time
import json
import errno
from collections import deque
from optparse import OptionParser
from threading import Thread, Lock
import serial
from flask import Flask, render_template, Response, jsonify
import folium
from folium import plugins
import numpy as np

app = Flask(__name__)

class SerialMapper:
    def __init__(self, port=None, baudrate=115200, max_points=None, use_stdin=False, dump_serial=False):
        """
        Initialize the serial mapper.

        Args:
            port: Serial port path (None if use_stdin=True)
            baudrate: Serial baud rate
            max_points: Maximum number of points to keep in memory (None = unlimited)
            use_stdin: If True, read from stdin instead of serial port
            dump_serial: If True, print all received lines to stdout
        """
        self.port = port
        self.baudrate = baudrate
        self.max_points = max_points
        self.use_stdin = use_stdin
        self.dump_serial = dump_serial

        # Data storage - use list for unlimited storage, deque for limited
        if max_points is None:
            self.data_points = []
        else:
            self.data_points = deque(maxlen=max_points)
        self.data_lock = Lock()
        self.running = False

        # Statistics
        self.good_samples = 0
        self.bad_samples = 0

        # Auto-zeroing for color scale
        self.calibration_samples = []
        self.calibration_count = 10  # Use first 10 samples to establish baseline
        self.value_range_fixed = None  # Will be set after calibration
        self.zero_value = None  # The calibrated zero point
        self.value_range_plus = 0.03  # Range above zero (adjustable)
        self.value_range_minus = 1.0  # Range below zero (adjustable)

        # Default map center (will be updated with first valid point)
        self.map_center = [37.7749, -122.4194]  # San Francisco default

        # Initialize input source
        if use_stdin:
            print("Reading from stdin...", flush=True)
            self.serial = None
            self.input_stream = sys.stdin
        else:
            # Initialize serial connection
            try:
                self.serial = serial.Serial(port, baudrate, timeout=0.1)
                print(f"Connected to {port} at {baudrate} baud", flush=True)
                self.serial.reset_input_buffer()
                time.sleep(0.1)

                # Synchronize with data stream
                print("Synchronizing with data stream...", flush=True)
                synced = False
                for attempt in range(10):
                    line = self.serial.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        # Ignore comment lines
                        if line.startswith('#'):
                            continue

                        try:
                            parts = line.split(',')
                            # Parse lat, lon, fix_quality, value (ignore any extra fields like timestamp)
                            if len(parts) >= 4:
                                lat = float(parts[0])
                                lon = float(parts[1])
                                fix_quality = int(parts[2])
                                value = float(parts[3])
                                # Basic sanity check for coordinates
                                if -90 <= lat <= 90 and -180 <= lon <= 180:
                                    synced = True
                                    print(f"Synchronized! First valid line: {line}", flush=True)
                                    self.map_center = [lat, lon]
                                    break
                        except ValueError:
                            continue

                if not synced:
                    print("Warning: Could not sync with data stream, using default map center", flush=True)

            except Exception as e:
                print(f"Error opening serial port: {e}")
                sys.exit(1)

    def reconnect_serial(self):
        """Attempt to reconnect to the serial port after disconnection."""
        if self.use_stdin:
            return False  # Can't reconnect to stdin

        print(f"Attempting to reconnect to {self.port}...", flush=True)

        while self.running:
            try:
                # Close old connection if it exists
                if self.serial and self.serial.is_open:
                    try:
                        self.serial.close()
                    except:
                        pass

                # Try to reopen the serial port
                self.serial = serial.Serial(self.port, self.baudrate, timeout=0.1)
                print(f"Reconnected to {self.port}!", flush=True)
                self.serial.reset_input_buffer()
                time.sleep(0.1)
                return True

            except (OSError, serial.SerialException) as e:
                # Still disconnected, wait and try again
                time.sleep(2)  # Wait 2 seconds between reconnection attempts

        return False

    def read_serial_data(self):
        """Background thread to continuously read serial data or stdin."""
        self.running = True
        while self.running:
            try:
                if self.use_stdin:
                    # Read from stdin
                    line = self.input_stream.readline()
                    if not line:  # EOF
                        print("End of stdin stream", flush=True)
                        break
                    line = line.strip()
                else:
                    # Read from serial port
                    if not self.serial.is_open:
                        break

                    if self.serial.in_waiting == 0:
                        time.sleep(0.01)
                        continue

                    line = self.serial.readline().decode('utf-8', errors='ignore').strip()

                # Debug: print first few lines
                if self.good_samples + self.bad_samples < 5:
                    print(f"DEBUG: Read line: '{line}'", flush=True)

                # Dump serial data if requested
                if self.dump_serial and line:
                    print(line, flush=True)

                if line:
                    # Ignore comment lines
                    if line.startswith('#'):
                        continue

                    try:
                        parts = line.split(',')
                        # Parse lat, lon, fix_quality, value (ignore any extra fields like timestamp)
                        if len(parts) >= 4:
                            lat = float(parts[0])
                            lon = float(parts[1])
                            fix_quality = int(parts[2])
                            value = float(parts[3])

                            # Validate coordinates
                            if -90 <= lat <= 90 and -180 <= lon <= 180:
                                data_point = {
                                    'lat': lat,
                                    'lon': lon,
                                    'fix_quality': fix_quality,
                                    'value': value,
                                    'timestamp': time.time()
                                }

                                with self.data_lock:
                                    self.data_points.append(data_point)
                                    # Update map center to first valid point
                                    if self.good_samples == 0:
                                        self.map_center = [lat, lon]

                                    # Collect calibration samples
                                    if self.value_range_fixed is None:
                                        self.calibration_samples.append(value)
                                        if len(self.calibration_samples) >= self.calibration_count:
                                            # Calculate average and set fixed range
                                            avg_value = sum(self.calibration_samples) / len(self.calibration_samples)
                                            self.zero_value = avg_value
                                            self.value_range_fixed = (
                                                avg_value - self.value_range_minus,
                                                avg_value + self.value_range_plus
                                            )
                                            print(f"Calibration complete! Zero: {avg_value:.3f}, Range: {self.value_range_fixed[0]:.3f} to {self.value_range_fixed[1]:.3f}", flush=True)

                                self.good_samples += 1

                                if self.good_samples == 1:
                                    print(f"First valid data - Lat: {lat}, Lon: {lon}, Value: {value}", flush=True)
                            else:
                                raise ValueError(f"Invalid coordinates: lat={lat}, lon={lon}")

                    except (ValueError, IndexError) as e:
                        self.bad_samples += 1
                        if self.bad_samples <= 10:
                            print(f"Error parsing line '{line}': {e}")
                        elif self.bad_samples == 11:
                            print(f"Suppressing further parse errors... (Good: {self.good_samples}, Bad: {self.bad_samples})")

            except (OSError, serial.SerialException) as e:
                # USB disconnection or serial port error
                if isinstance(e, OSError) and e.errno == errno.ENOTTY:
                    # Device not configured - USB unplugged
                    print(f"USB device disconnected: {e}", flush=True)
                elif isinstance(e, OSError) and e.errno == 6:
                    # Errno 6 specifically
                    print(f"Device not configured (USB unplugged): {e}", flush=True)
                else:
                    print(f"Serial port error: {e}", flush=True)

                # Attempt to reconnect
                if not self.reconnect_serial():
                    break  # Reconnection failed or running=False

            except Exception as e:
                print(f"Error reading data: {e}", flush=True)
                time.sleep(0.1)

    def get_data_snapshot(self):
        """Get a thread-safe snapshot of current data points."""
        with self.data_lock:
            return list(self.data_points)

    def get_value_range(self):
        """Get fixed value range for color scaling (established during calibration)."""
        with self.data_lock:
            if self.value_range_fixed is None:
                # Still calibrating, return temporary range
                if len(self.calibration_samples) > 0:
                    avg = sum(self.calibration_samples) / len(self.calibration_samples)
                    return avg - self.value_range_minus, avg + self.value_range_plus
                return 0, 1
            return self.value_range_fixed

    def update_range_sensitivity(self, plus_value, minus_value):
        """Update the color scale sensitivity ranges."""
        with self.data_lock:
            self.value_range_plus = plus_value
            self.value_range_minus = minus_value
            # Recalculate range if we have a zero point
            if self.zero_value is not None:
                self.value_range_fixed = (
                    self.zero_value - self.value_range_minus,
                    self.zero_value + self.value_range_plus
                )
                print(f"Range updated! Zero: {self.zero_value:.3f}, Range: {self.value_range_fixed[0]:.3f} to {self.value_range_fixed[1]:.3f}", flush=True)
                return True
            return False

    def recalibrate(self):
        """Re-zero the color scale using the last 10 samples."""
        with self.data_lock:
            if len(self.data_points) >= 10:
                # Get last 10 values
                recent_values = [p['value'] for p in list(self.data_points)[-10:]]
                avg_value = sum(recent_values) / len(recent_values)
                self.zero_value = avg_value
                self.value_range_fixed = (
                    avg_value - self.value_range_minus,
                    avg_value + self.value_range_plus
                )
                print(f"Re-calibration complete! New zero: {avg_value:.3f}, Range: {self.value_range_fixed[0]:.3f} to {self.value_range_fixed[1]:.3f}", flush=True)
                return True
            return False

    def cleanup(self):
        """Stop reading and close serial port."""
        self.running = False
        if self.serial and self.serial.is_open:
            self.serial.close()
            print("Serial port closed")

# Global mapper instance
mapper = None

@app.route('/')
def index():
    """Serve the main map page."""
    # Create HTML page with Leaflet map
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Metal Mapper - Live View</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <!-- Leaflet CSS -->
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
              integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
              crossorigin=""/>

        <style>
            body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
            #map {{ position: absolute; top: 110px; left: 0; right: 0; bottom: 0; }}
            #info {{
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 110px;
                background: rgba(0,0,0,0.8);
                color: white;
                padding: 10px;
                z-index: 1000;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 15px;
            }}
            #legend {{ font-size: 12px; }}
            .button-stack {{
                display: flex;
                flex-direction: column;
                gap: 5px;
            }}
            #pauseBtn, #recalibrateBtn, #clearDataBtn {{
                background: #4CAF50;
                color: white;
                border: none;
                padding: 6px 16px;
                cursor: pointer;
                border-radius: 4px;
                font-size: 13px;
                font-weight: bold;
            }}
            #pauseBtn {{
                padding: 6px 8px;
                min-width: 35px;
            }}
            #pauseBtn:hover, #recalibrateBtn:hover {{
                background: #45a049;
            }}
            #pauseBtn.paused {{
                background: #f44336;
            }}
            #pauseBtn.paused:hover {{
                background: #da190b;
            }}
            #recalibrateBtn {{
                background: #2196F3;
            }}
            #recalibrateBtn:hover {{
                background: #0b7dda;
            }}
            #clearDataBtn {{
                background: #f44336;
            }}
            #clearDataBtn:hover {{
                background: #da190b;
            }}
            .slider-container {{
                display: flex;
                flex-direction: column;
                gap: 5px;
            }}
            .slider-control {{
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 12px;
            }}
            .slider-control label {{
                min-width: 60px;
            }}
            .slider-control input[type="range"] {{
                width: 150px;
                height: 4px;
            }}
            .slider-control .value {{
                min-width: 40px;
                font-family: monospace;
            }}
            .range-control-container {{
                display: flex;
                flex-direction: column;
                gap: 8px;
                padding: 8px;
                background: rgba(255,255,255,0.1);
                border-radius: 5px;
            }}
            .range-control {{
                width: 600px;
                height: 15px;
                position: relative;
                border: 2px solid #fff;
                border-radius: 5px;
                user-select: none;
            }}
            .zero-marker {{
                position: absolute;
                width: 3px;
                height: 100%;
                background: white;
                border: 1px solid black;
                top: 0;
                z-index: 10;
                pointer-events: none;
            }}
            .value-indicator {{
                position: absolute;
                width: 2px;
                height: 100%;
                background: rgba(255, 255, 0, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.7);
                top: 0;
                z-index: 15;
                pointer-events: none;
                transition: left 0.1s ease-out;
            }}
            .sliders-and-last {{
                display: flex;
                gap: 15px;
                align-items: center;
            }}
            .sliders-group {{
                display: flex;
                flex-direction: column;
                gap: 4px;
            }}
            .right-side-controls {{
                display: flex;
                flex-direction: column;
                align-items: flex-start;
            }}
            #fix-quality-display {{
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 5px 15px;
                background: rgba(0,0,0,0.3);
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }}
            #fix-quality-text {{
                color: #ffff00;
            }}
            .info-block {{
                display: flex;
                flex-direction: column;
                gap: 3px;
                font-size: 12px;
            }}
            .info-line {{
                display: flex;
                gap: 10px;
            }}
            .last-value-display {{
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 2px;
                padding: 0 10px;
            }}
            #last-value {{
                font-size: 28px;
                font-family: 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Mono', 'Droid Sans Mono', 'Source Code Pro', 'Courier New', monospace;
                font-weight: bold;
                text-shadow: 0 0 5px rgba(0, 0, 0, 0.5);
                letter-spacing: 0.05em;
            }}
        </style>

        <!-- Leaflet JavaScript -->
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
                integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
                crossorigin=""></script>
    </head>
    <body>
        <div id="info">
            <div class="button-stack">
                <button id="recalibrateBtn" onclick="recalibrate()">🎯 RE-ZERO</button>
                <button id="clearDataBtn" onclick="clearData()">🗑️ CLEAR DATA</button>
            </div>
            <button id="pauseBtn" onclick="togglePause()">⏸</button>
            <div class="range-control-container">
                <div class="range-control" id="rangeControl">
                    <div class="zero-marker" id="zeroMarker"></div>
                    <div class="value-indicator" id="valueIndicator"></div>
                </div>
                <div class="sliders-and-last">
                    <div class="sliders-group">
                        <div class="slider-control">
                            <label>- Range</label>
                            <input type="range" id="minusRangeSlider" min="0.01" max="5" step="0.01" value="1.0" oninput="updateMinusRange()">
                            <span class="value" id="minusRangeValue">1.00</span>
                        </div>
                        <div class="slider-control">
                            <label>+ Range</label>
                            <input type="range" id="plusRangeSlider" min="0.01" max="5" step="0.01" value="0.03" oninput="updatePlusRange()">
                            <span class="value" id="plusRangeValue">0.03</span>
                        </div>
                        <div class="slider-control">
                            <label>Offset</label>
                            <input type="range" id="offsetSlider" min="-2" max="2" step="0.01" value="0" oninput="updateOffset()">
                            <span class="value" id="offsetValue">0.000</span>
                        </div>
                    </div>
                    <div class="last-value-display">
                        <div style="font-size: 11px; color: #aaa;">Last:</div>
                        <div><span id="last-value">-</span><span style="font-size: 16px; margin-left: 4px;">μs</span></div>
                    </div>
                </div>
            </div>
            <div class="right-side-controls">
                <div style="display: flex; gap: 20px; align-items: center;">
                    <div class="info-block">
                        <div>Points: <span id="point-count">0</span> | Zoom: <span id="zoom-level">20</span></div>
                        <div class="info-line">
                            <span>Zero: <span id="zero-value">-</span></span>
                            <span>Range: <span id="value-range">-</span></span>
                        </div>
                    </div>
                    <div id="fix-quality-display">
                        <span id="fix-quality-text">No Fix</span>
                    </div>
                </div>
                <div class="slider-control" style="margin-top: 8px;">
                    <label>Size</label>
                    <input type="range" id="sizeSlider" min="1" max="6" step="1" value="2" oninput="updateDotSize()">
                    <span class="value" id="sizeValue">2</span>
                </div>
            </div>
        </div>
        <div id="map"></div>

        <script>
            // Initialize the map with high zoom for precision GPS
            const mapObj = L.map('map', {{
                maxZoom: 25,
                zoomControl: true
            }}).setView([{mapper.map_center[0]}, {mapper.map_center[1]}], 25);

            // Add OpenStreetMap tiles with max native zoom
            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                maxZoom: 25,
                maxNativeZoom: 19,  // OSM tiles only go to 19, but we can oversample
                attribution: '© OpenStreetMap contributors'
            }}).addTo(mapObj);

            // Add scale control to show distances
            L.control.scale({{
                imperial: true,
                metric: true,
                maxWidth: 200
            }}).addTo(mapObj);

            // Load and display GeoJSON camp outlines
            fetch('/geojson/camp_outlines_2025.geojson')
                .then(response => response.json())
                .then(data => {{
                    L.geoJSON(data, {{
                        style: {{
                            color: '#FF8800',
                            weight: 2,
                            opacity: 0.6,
                            fillOpacity: 0
                        }},
                        onEachFeature: function(feature, layer) {{
                            if (feature.properties && feature.properties.Layer) {{
                                layer.bindPopup(feature.properties.Layer);
                            }}
                        }}
                    }}).addTo(mapObj);
                    console.log('Camp outlines loaded');
                }})
                .catch(error => {{
                    console.error('Error loading camp outlines:', error);
                }});

            // Update zoom level display
            mapObj.on('zoomend', function() {{
                document.getElementById('zoom-level').textContent = mapObj.getZoom();
            }});

            // Store markers and layers
            let markers = [];
            let polyline = null;
            let lastPointCount = 0;
            const POPUP_THRESHOLD = 100;  // Only add popups to last 100 points
            let isPaused = false;
            let currentZeroValue = null;  // Track the calibrated zero point
            let dotSize = 2;  // Current dot size (2-6)
            let frozenData = null;  // Store frozen data when paused
            let manualZeroAdjustment = 0;  // Manual zero point adjustment
            let plusRange = 0.03;  // Plus range
            let minusRange = 1.0;  // Minus range

            // Display window for 120% range
            let displayMin = 0;
            let displayMax = 1;
            const CONTROL_WIDTH = 600;  // pixels
            let lastReadingValue = null;  // Track latest value for indicator

            // Toggle pause/resume
            function togglePause() {{
                isPaused = !isPaused;
                const btn = document.getElementById('pauseBtn');

                if (isPaused) {{
                    btn.textContent = '▶';
                    btn.classList.add('paused');
                    // Freeze current data
                    fetch('/data')
                        .then(response => response.json())
                        .then(data => {{
                            frozenData = data;
                            console.log('Data frozen:', frozenData.points.length, 'points');
                        }})
                        .catch(error => console.error('Error:', error));
                }} else {{
                    btn.textContent = '⏸';
                    btn.classList.remove('paused');
                    frozenData = null;
                }}
            }}

            // Re-calibrate color scale
            function recalibrate() {{
                fetch('/recalibrate', {{ method: 'POST' }})
                    .then(response => response.json())
                    .then(data => {{
                        if (data.success) {{
                            console.log('Re-calibration successful:', data);
                            // Reset manual zero adjustment and offset slider
                            manualZeroAdjustment = 0;
                            document.getElementById('offsetSlider').value = 0;
                            // Display absolute zero value
                            const absoluteValue = currentZeroValue !== null ? currentZeroValue : 0;
                            document.getElementById('offsetValue').textContent = absoluteValue.toFixed(3);
                            updateRangeControl();
                        }} else {{
                            console.error('Re-calibration failed:', data.error);
                        }}
                    }})
                    .catch(error => {{
                        console.error('Error:', error);
                    }});
            }}

            // Clear all data points
            function clearData() {{
                fetch('/clear_data', {{ method: 'POST' }})
                    .then(response => response.json())
                    .then(data => {{
                        if (data.success) {{
                            console.log('Data cleared successfully');
                            // Clear map markers
                            markers.forEach(marker => mapObj.removeLayer(marker));
                            markers = [];
                            if (polyline) {{
                                mapObj.removeLayer(polyline);
                                polyline = null;
                            }}
                            lastReadingValue = null;
                        }} else {{
                            console.error('Clear data failed:', data.error);
                        }}
                    }})
                    .catch(error => {{
                        console.error('Error:', error);
                    }});
            }}

            // Update minus range from slider
            function updateMinusRange() {{
                minusRange = parseFloat(document.getElementById('minusRangeSlider').value);
                document.getElementById('minusRangeValue').textContent = minusRange.toFixed(2);
                updateRange();
                updateRangeControl();
            }}

            // Update plus range from slider
            function updatePlusRange() {{
                plusRange = parseFloat(document.getElementById('plusRangeSlider').value);
                document.getElementById('plusRangeValue').textContent = plusRange.toFixed(2);
                updateRange();
                updateRangeControl();
            }}

            // Update offset from slider
            function updateOffset() {{
                manualZeroAdjustment = parseFloat(document.getElementById('offsetSlider').value);
                // Display absolute value (zero + offset)
                const absoluteValue = currentZeroValue !== null ? (currentZeroValue + manualZeroAdjustment) : manualZeroAdjustment;
                document.getElementById('offsetValue').textContent = absoluteValue.toFixed(3);
                updateRangeControl();
                // Re-render markers with new offset
                if (isPaused && frozenData) {{
                    renderMarkers(frozenData);
                    if (frozenData.points.length > 0) {{
                        lastReadingValue = frozenData.points[frozenData.points.length - 1].value;
                        updateValueIndicator();
                    }}
                }} else {{
                    fetch('/data')
                        .then(response => response.json())
                        .then(currentData => {{
                            renderMarkers(currentData);
                            if (currentData.points.length > 0) {{
                                lastReadingValue = currentData.points[currentData.points.length - 1].value;
                                updateValueIndicator();
                            }}
                        }})
                        .catch(error => console.error('Error:', error));
                }}
            }}

            // Update range via API
            function updateRange() {{
                fetch('/update_range', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ plus: plusRange, minus: minusRange }})
                }})
                .then(response => response.json())
                .then(data => {{
                    if (data.success) {{
                        console.log('Range updated:', data);
                        // Re-render current data
                        if (isPaused && frozenData) {{
                            frozenData.value_min = data.range[0];
                            frozenData.value_max = data.range[1];
                            renderMarkers(frozenData);
                            if (frozenData.points.length > 0) {{
                                const lastValue = frozenData.points[frozenData.points.length - 1].value;
                                lastReadingValue = lastValue;
                                const adjustedZero = currentZeroValue + manualZeroAdjustment;
                                const lastColor = getColor(lastValue, frozenData.value_min, frozenData.value_max, adjustedZero);
                                document.getElementById('last-value').style.color = lastColor;
                                updateValueIndicator();
                            }}
                        }} else {{
                            fetch('/data')
                                .then(response => response.json())
                                .then(currentData => {{
                                    renderMarkers(currentData);
                                    if (currentData.points.length > 0) {{
                                        const lastValue = currentData.points[currentData.points.length - 1].value;
                                        lastReadingValue = lastValue;
                                        const adjustedZero = currentZeroValue + manualZeroAdjustment;
                                        const lastColor = getColor(lastValue, currentData.value_min, currentData.value_max, adjustedZero);
                                        document.getElementById('last-value').style.color = lastColor;
                                        updateValueIndicator();
                                    }}
                                }})
                                .catch(error => console.error('Error:', error));
                        }}
                    }}
                }})
                .catch(error => console.error('Error:', error));
            }}

            // Update dot size
            function updateDotSize() {{
                dotSize = parseInt(document.getElementById('sizeSlider').value);
                document.getElementById('sizeValue').textContent = dotSize;
                // Re-render current data
                if (isPaused && frozenData) {{
                    renderMarkers(frozenData);
                }} else {{
                    // Fetch and render current live data
                    fetch('/data')
                        .then(response => response.json())
                        .then(currentData => {{
                            renderMarkers(currentData);
                        }})
                        .catch(error => console.error('Error:', error));
                }}
            }}

            // Update range control visualization with 50/50 split
            function updateRangeControl() {{
                if (currentZeroValue === null) return;

                const adjustedZero = currentZeroValue + manualZeroAdjustment;

                // 50/50 split: left half = minusRange, right half = plusRange
                // This creates different scaling on each side
                displayMin = adjustedZero - minusRange;
                displayMax = adjustedZero + plusRange;

                // Position zero marker at center (50%)
                const zeroPercent = 50;
                document.getElementById('zeroMarker').style.left = (zeroPercent / 100 * CONTROL_WIDTH) + 'px';

                // Update gradient - green to grey at center to red
                const rangeControl = document.getElementById('rangeControl');
                rangeControl.style.background = `linear-gradient(to right, #00ff00, #808080 ${{zeroPercent}}%, #ff0000)`;

                // Update value indicator if we have a reading
                if (lastReadingValue !== null) {{
                    updateValueIndicator();
                }}
            }}

            // Update the position of the yellow value indicator line
            function updateValueIndicator() {{
                if (lastReadingValue === null || (displayMin === 0 && displayMax === 1)) return;

                const adjustedZero = currentZeroValue + manualZeroAdjustment;

                let valuePercent;
                if (lastReadingValue <= adjustedZero) {{
                    // Left side: map from displayMin (0%) to adjustedZero (50%)
                    const leftRange = adjustedZero - displayMin;
                    const leftPosition = lastReadingValue - displayMin;
                    valuePercent = leftRange > 0 ? (leftPosition / leftRange) * 50 : 50;
                }} else {{
                    // Right side: map from adjustedZero (50%) to displayMax (100%)
                    const rightRange = displayMax - adjustedZero;
                    const rightPosition = lastReadingValue - adjustedZero;
                    valuePercent = rightRange > 0 ? 50 + (rightPosition / rightRange) * 50 : 50;
                }}

                const clampedPercent = Math.max(0, Math.min(100, valuePercent));
                document.getElementById('valueIndicator').style.left = (clampedPercent / 100 * CONTROL_WIDTH) + 'px';
            }}

            // Map fix quality integer to string
            function getFixQualityText(fixQuality) {{
                const fixLabels = ['No Fix', '2D', '3D', 'PPS', 'RTK Fixed', 'RTK Float'];
                return fixLabels[fixQuality] || 'No Fix';
            }}

            // Color interpolation for values
            // Green at min (zero - minus_range), Grey at zero, Red at max (zero + plus_range)
            function getColor(value, minVal, maxVal, zeroPoint) {{
                // Use actual calibrated zero point, not midpoint
                if (zeroPoint === null || zeroPoint === undefined) {{
                    zeroPoint = (minVal + maxVal) / 2;  // Fallback
                }}

                const minusRange = zeroPoint - minVal;
                const plusRange = maxVal - zeroPoint;

                if (value <= zeroPoint) {{
                    // Below or at zero: interpolate from green (#00ff00) to grey (#808080)
                    // normalized = 0 at minVal, normalized = 1 at zeroPoint
                    const normalized = minusRange > 0 ? (value - minVal) / minusRange : 0;
                    const r = Math.round(0 + normalized * 128);      // 0 → 128
                    const g = Math.round(255 - normalized * 127);    // 255 → 128
                    const b = Math.round(0 + normalized * 128);      // 0 → 128
                    return `rgb(${{r}}, ${{g}}, ${{b}})`;
                }} else {{
                    // Above zero: interpolate from grey (#808080) to red (#ff0000)
                    // normalized = 0 at zeroPoint, normalized = 1 at maxVal
                    const normalized = plusRange > 0 ? (value - zeroPoint) / plusRange : 0;
                    const r = Math.round(128 + normalized * 127);    // 128 → 255
                    const g = Math.round(128 - normalized * 128);    // 128 → 0
                    const b = Math.round(128 - normalized * 128);    // 128 → 0
                    return `rgb(${{r}}, ${{g}}, ${{b}})`;
                }}
            }}

            // Render markers on the map
            function renderMarkers(data) {{
                // Clear old markers
                markers.forEach(marker => mapObj.removeLayer(marker));
                markers = [];

                // Remove old polyline
                if (polyline) {{
                    mapObj.removeLayer(polyline);
                }}

                if (data.points.length === 0) return;

                // Add polyline connecting all points (thin, subtle)
                const latlngs = data.points.map(p => [p.lat, p.lon]);
                polyline = L.polyline(latlngs, {{
                    color: '#4444ff',
                    weight: 1,
                    opacity: 0.4
                }}).addTo(mapObj);

                // Add markers for each point
                data.points.forEach((point, index) => {{
                    const adjustedZero = currentZeroValue + manualZeroAdjustment;
                    const color = getColor(point.value, data.value_min, data.value_max, adjustedZero);
                    const isLatest = index === data.points.length - 1;
                    const isRecent = index >= data.points.length - POPUP_THRESHOLD;

                    // Use dynamic dot size
                    const baseRadius = dotSize;
                    const latestRadius = Math.round(baseRadius * 2);

                    const marker = L.circleMarker([point.lat, point.lon], {{
                        radius: isLatest ? latestRadius : baseRadius,
                        fillColor: color,
                        color: isLatest ? '#fff' : color,
                        weight: isLatest ? 1.5 : 0.3,
                        opacity: 1,
                        fillOpacity: isLatest ? 1.0 : 0.7
                    }}).addTo(mapObj);

                    // Only add popups to recent points for performance
                    if (isRecent) {{
                        marker.bindPopup(`Value: ${{point.value.toFixed(2)}}<br>Lat: ${{point.lat.toFixed(6)}}<br>Lon: ${{point.lon.toFixed(6)}}`);
                    }}
                    markers.push(marker);
                }});
            }}

            // Update map data via Server-Sent Events
            const eventSource = new EventSource('/stream');

            eventSource.onmessage = function(event) {{
                // If paused, ignore new data completely
                if (isPaused) {{
                    return;
                }}

                const data = JSON.parse(event.data);

                // Update statistics
                document.getElementById('point-count').textContent = data.points.length;
                document.getElementById('value-range').textContent =
                    data.value_min.toFixed(2) + ' - ' + data.value_max.toFixed(2);

                // Update fix quality
                if (data.fix_quality !== undefined) {{
                    const fixQualityElement = document.getElementById('fix-quality-text');
                    fixQualityElement.textContent = getFixQualityText(data.fix_quality);
                    // Red if No Fix (0), Green if RTK Fixed (4) or RTK Float (5), Yellow otherwise
                    if (data.fix_quality === 0) {{
                        fixQualityElement.style.color = '#ff0000';
                    }} else if (data.fix_quality === 4 || data.fix_quality === 5) {{
                        fixQualityElement.style.color = '#00ff00';
                    }} else {{
                        fixQualityElement.style.color = '#ffff00';
                    }}
                }}

                // Update last value and zero value
                if (data.points.length > 0) {{
                    const lastValue = data.points[data.points.length - 1].value;
                    lastReadingValue = lastValue;  // Track for value indicator
                    const lastValueElement = document.getElementById('last-value');
                    lastValueElement.textContent = lastValue.toFixed(3);
                    // Color the last value based on current ranges with manual adjustment
                    const adjustedZero = currentZeroValue + manualZeroAdjustment;
                    const lastColor = getColor(lastValue, data.value_min, data.value_max, adjustedZero);
                    lastValueElement.style.color = lastColor;
                    // Update value indicator position
                    updateValueIndicator();
                }}
                if (data.zero_value !== undefined && data.zero_value !== null) {{
                    currentZeroValue = data.zero_value;
                    document.getElementById('zero-value').textContent = data.zero_value.toFixed(3);
                    // Update offset display with absolute value
                    const absoluteValue = currentZeroValue + manualZeroAdjustment;
                    document.getElementById('offsetValue').textContent = absoluteValue.toFixed(3);
                    updateRangeControl();
                }}

                // Render markers
                renderMarkers(data);

                // Pan to latest point if new data arrived
                if (data.points.length > lastPointCount) {{
                    const latest = data.points[data.points.length - 1];
                    mapObj.panTo([latest.lat, latest.lon], {{
                        animate: true,
                        duration: 0.25
                    }});
                }}

                lastPointCount = data.points.length;
            }};

            eventSource.onerror = function(event) {{
                console.error('EventSource error:', event);
            }};

            console.log('Map initialized and listening for data...');
        </script>
    </body>
    </html>
    """
    return html

@app.route('/stream')
def stream():
    """Server-Sent Events stream for live updates."""
    def event_stream():
        while mapper.running:
            # Get current data
            points = mapper.get_data_snapshot()
            value_min, value_max = mapper.get_value_range()

            # Get latest fix quality
            latest_fix_quality = points[-1]['fix_quality'] if points else 0

            data = {
                'points': [{'lat': p['lat'], 'lon': p['lon'], 'value': p['value'], 'fix_quality': p.get('fix_quality', 0)} for p in points],
                'good_samples': mapper.good_samples,
                'bad_samples': mapper.bad_samples,
                'value_min': value_min,
                'value_max': value_max,
                'zero_value': mapper.zero_value,
                'fix_quality': latest_fix_quality
            }

            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.2)  # Update 5 times per second (good balance for 20 Hz input)

    return Response(event_stream(), mimetype='text/event-stream')

@app.route('/data')
def get_data():
    """JSON endpoint for current data (alternative to SSE)."""
    points = mapper.get_data_snapshot()
    value_min, value_max = mapper.get_value_range()

    # Get latest fix quality
    latest_fix_quality = points[-1]['fix_quality'] if points else 0

    return jsonify({
        'points': [{'lat': p['lat'], 'lon': p['lon'], 'value': p['value'], 'fix_quality': p.get('fix_quality', 0)} for p in points],
        'good_samples': mapper.good_samples,
        'bad_samples': mapper.bad_samples,
        'value_min': value_min,
        'value_max': value_max,
        'zero_value': mapper.zero_value,
        'fix_quality': latest_fix_quality
    })

@app.route('/geojson/camp_outlines_2025.geojson')
def get_camp_outlines():
    """Serve the camp outlines GeoJSON file."""
    import os
    from flask import send_file
    geojson_path = os.path.join(os.path.dirname(__file__), 'camp_outlines_2025.geojson')
    return send_file(geojson_path, mimetype='application/json')

@app.route('/recalibrate', methods=['POST'])
def recalibrate():
    """Re-calibrate the color scale using the last 10 samples."""
    success = mapper.recalibrate()
    if success:
        value_min, value_max = mapper.get_value_range()
        return jsonify({
            'success': True,
            'range': [value_min, value_max]
        })
    else:
        return jsonify({
            'success': False,
            'error': 'Not enough data points (need at least 10)'
        })

@app.route('/update_range', methods=['POST'])
def update_range():
    """Update the color scale sensitivity ranges."""
    from flask import request
    data = request.get_json()
    plus_val = float(data.get('plus', 1.0))
    minus_val = float(data.get('minus', 1.0))

    success = mapper.update_range_sensitivity(plus_val, minus_val)
    value_min, value_max = mapper.get_value_range()

    return jsonify({
        'success': True,
        'range': [value_min, value_max]
    })

@app.route('/clear_data', methods=['POST'])
def clear_data():
    """Clear all data points and reset counters."""
    with mapper.data_lock:
        if isinstance(mapper.data_points, list):
            mapper.data_points.clear()
        else:
            mapper.data_points.clear()
        mapper.good_samples = 0
        mapper.bad_samples = 0

    return jsonify({
        'success': True
    })

def main():
    parser = OptionParser(usage="usage: %prog [options]")
    parser.add_option("-p", "--port", dest="port",
                      default="/dev/cu.usbmodem1402",
                      help="Serial port path [default: %default]")
    parser.add_option("-b", "--baudrate", dest="baudrate",
                      type="int", default=460800,
                      help="Baud rate [default: %default]")
    parser.add_option("-m", "--max-points", dest="max_points",
                      type="int", default=0,
                      help="Maximum points to keep (0=unlimited) [default: unlimited]")
    parser.add_option("-w", "--web-port", dest="web_port",
                      type="int", default=5000,
                      help="Web server port [default: %default]")
    parser.add_option("--stdin", dest="use_stdin",
                      action="store_true", default=False,
                      help="Read from stdin instead of serial port")
    parser.add_option("-d", "--dump", dest="dump_serial",
                      action="store_true", default=False,
                      help="Dump all received serial data to stdout")

    (options, args) = parser.parse_args()

    # Convert 0 to None for unlimited points
    max_points = None if options.max_points == 0 else options.max_points

    # Create mapper instance
    global mapper
    if options.use_stdin:
        mapper = SerialMapper(max_points=max_points, use_stdin=True, dump_serial=options.dump_serial)
    else:
        mapper = SerialMapper(options.port, options.baudrate, max_points, dump_serial=options.dump_serial)

    # Start serial reading thread
    serial_thread = Thread(target=mapper.read_serial_data, daemon=True)
    serial_thread.start()

    print(f"\nStarting web server on http://localhost:{options.web_port}")
    print("Open this URL in your browser to view the live map")
    print("Press Ctrl+C to stop\n")

    try:
        # Start Flask web server
        app.run(host='0.0.0.0', port=options.web_port, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        mapper.cleanup()

if __name__ == '__main__':
    main()
