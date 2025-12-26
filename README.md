# Metal Mapper

Real-time visualization tools for metal detection data with GPS coordinates.

## Files

- **plot_serial_stream.py** - Real-time plotter for charge/discharge timing data using PyQtGraph
- **map_serial_stream.py** - Real-time GPS mapper for metal detection data using Folium
- **test_data_generator.py** - Test data generator for development and testing

## Installation

Install required packages:

```bash
pip install -r requirements.txt
```

Additional package for the PyQtGraph plotter:
```bash
pip install pyqtgraph PyQt6
```

## Usage

### GPS Map Viewer (map_serial_stream.py)

Reads serial data in format: `lat,lon,value`

```bash
# Basic usage with default port
python map_serial_stream.py

# Specify serial port and baudrate
python map_serial_stream.py -p /dev/cu.usbmodem1402 -b 115200

# Custom web server port
python map_serial_stream.py -w 8080

# Limit maximum points kept in memory (default is unlimited)
python map_serial_stream.py -m 5000

# Read from stdin instead of serial port (useful for testing)
python test_data_generator.py 2>/dev/null | python map_serial_stream.py --stdin
```

Then open your browser to: http://localhost:5000

### Timing Plotter (plot_serial_stream.py)

Reads serial data in format: `charge_time,discharge_time`

```bash
# Basic usage
python plot_serial_stream.py

# Specify serial port and window size
python plot_serial_stream.py -p /dev/cu.usbmodem1402 -w 20
```

### Test Data Generator

Generate simulated GPS tracking data for testing:

```bash
# Generate infinite test data on stdout
python test_data_generator.py

# Generate 100 points
python test_data_generator.py -n 100

# Custom starting location (San Francisco)
python test_data_generator.py --lat 37.7749 --lon -122.4194

# Custom value range
python test_data_generator.py --value-min 0 --value-max 500

# Larger movement steps (more spread out)
python test_data_generator.py -s 0.0005
```

## Testing with Test Data

### Option 1: Using stdin (simplest)

```bash
# Pipe test data directly into the mapper
python test_data_generator.py | python map_serial_stream.py --stdin
```

### Option 2: Using Virtual Serial Ports

On macOS/Linux, you can test the mapper without hardware using `socat`:

```bash
# Install socat (macOS)
brew install socat

# Create virtual serial port pair
socat -d -d pty,raw,echo=0 pty,raw,echo=0
# This will output two paths like /dev/ttys001 and /dev/ttys002

# In one terminal, generate test data to one port
python test_data_generator.py > /dev/ttys001

# In another terminal, run the mapper on the other port
python map_serial_stream.py -p /dev/ttys002
```

## Data Format

### GPS Mapper Input Format
```
latitude,longitude,value
37.774929,-122.419416,42.5
37.774935,-122.419420,38.2
37.774941,-122.419425,55.7
```

- **latitude**: Decimal degrees, -90 to 90
- **longitude**: Decimal degrees, -180 to 180
- **value**: Floating point value (e.g., metal detection strength)

### Timing Plotter Input Format
```
charge_time,discharge_time
123.456,234.567
125.123,232.890
```

- **charge_time**: Time in microseconds
- **discharge_time**: Time in microseconds

## Features

### GPS Mapper
- Real-time plotting on interactive map
- Browser-based interface (works as local GUI or web server)
- Tracks path with polyline
- Color visualization based on value (blue=low, red=high)
- Ultra high-precision zoom (up to zoom level 25, ~1-2 meter view)
- GeoJSON overlay support (displays camp_outlines_2025.geojson)
- Pause/Resume control to freeze display for inspection
- Scale bar showing distances
- Fullscreen and measurement tools
- Server-Sent Events for live updates
- Optimized for high-frequency data (20 Hz)
- Unlimited point storage (all points kept per session)
- Optional memory limit with -m flag

### Timing Plotter
- Dual Y-axis real-time plotting
- Auto-scaling with fixed time window
- PyQtGraph for high-performance rendering
- Color-coded traces (green=charge, red=discharge)
- Current values displayed in title

## Future Enhancements

- [ ] Add color gradient based on value ranges
- [ ] Variable marker sizes based on value
- [ ] Export data to CSV/KML
- [ ] Heatmap overlay mode
- [ ] Data filtering and smoothing
- [ ] Multiple serial device support
- [ ] Recording and playback
