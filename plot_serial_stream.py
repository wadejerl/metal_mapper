#!/usr/bin/env python3
"""
Real-time serial data plotter using PyQtGraph.
Reads comma-separated values from serial port and plots them with scrolling display.
"""
import sys
import time
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from optparse import OptionParser
import serial

class SerialPlotter:
    def __init__(self, port, baudrate=115200, window_seconds=10):
        """
        Initialize the serial plotter.

        Args:
            port: Serial port path (e.g., '/dev/cu.usbmodem1402')
            baudrate: Serial baud rate
            window_seconds: Time window to display in seconds
        """
        self.port = port
        self.baudrate = baudrate
        self.window_seconds = window_seconds

        # Data buffers
        self.timestamps = []
        self.data1 = []
        self.data2 = []
        self.start_time = None
        self.sample_count = 0
        self.good_samples = 0
        self.bad_samples = 0

        # Initialize serial connection
        try:
            self.serial = serial.Serial(port, baudrate, timeout=0.5)
            print(f"Connected to {port} at {baudrate} baud", flush=True)
            # Flush buffer to sync with data stream
            self.serial.reset_input_buffer()
            time.sleep(0.1)

            # Discard partial lines and sync with valid data
            print("Synchronizing with data stream...", flush=True)
            synced = False
            for attempt in range(10):
                line = self.serial.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    try:
                        parts = line.split(',')
                        if len(parts) >= 2:
                            float(parts[0])
                            float(parts[1])
                            synced = True
                            print(f"Synchronized! First valid line: {line}", flush=True)
                            break
                    except ValueError:
                        continue

            if not synced:
                print("Warning: Could not sync with data stream, continuing anyway...", flush=True)

            # Reset timeout for normal operation
            self.serial.timeout = 0.1
        except Exception as e:
            print(f"Error opening serial port: {e}")
            sys.exit(1)

        # Setup PyQtGraph
        pg.setConfigOption('background', 'k')
        pg.setConfigOption('foreground', 'w')

        # Create main window
        self.win = pg.GraphicsLayoutWidget(show=True, title="Charge/Discharge Time Monitor")
        self.win.resize(1400, 800)

        # Create single plot with dual Y-axes
        self.p1 = self.win.addPlot(row=0, col=0)
        self.p1.setLabel('left', 'Charge Time (µs)', color='#00ff00')
        self.p1.setLabel('bottom', 'Time (seconds)')
        self.p1.showGrid(x=True, y=True, alpha=0.3)

        # Initialize title with larger font
        self.p1.setTitle("Charge and Discharge Times", size='18pt')

        # Create second Y-axis on the right
        self.p2 = pg.ViewBox()
        self.p1.showAxis('right')
        self.p1.scene().addItem(self.p2)
        self.p1.getAxis('right').linkToView(self.p2)
        self.p2.setXLink(self.p1)
        self.p1.getAxis('right').setLabel('Discharge Time (µs)', color='#ff0000')

        # Handle view resizing
        def updateViews():
            self.p2.setGeometry(self.p1.vb.sceneBoundingRect())
            self.p2.linkedViewChanged(self.p1.vb, self.p2.XAxis)

        updateViews()
        self.p1.vb.sigResized.connect(updateViews)

        # Create plot curves with different colors
        color1 = '#00ff00'  # Green for charge time
        self.curve1 = self.p1.plot(pen=pg.mkPen(color=color1, width=2.0))

        color2 = '#ff0000'  # Red for discharge time
        self.curve2 = pg.PlotCurveItem(pen=pg.mkPen(color=color2, width=2.0))
        self.p2.addItem(self.curve2)

        # Setup timer for reading serial data
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(10)  # Update every 10ms

    def update_plot(self):
        """Read serial data and update plots."""
        try:
            # Check if serial port is still open
            if not self.serial.is_open:
                return

            # Read all available lines
            while self.serial.in_waiting > 0:
                line = self.serial.readline().decode('utf-8').strip()

                # Debug: print raw line for first 5 reads
                if self.good_samples + self.bad_samples < 5:
                    print(f"DEBUG: Read line: '{line}'", flush=True)

                if line:
                    # Parse comma-separated values
                    try:
                        values = line.split(',')
                        if len(values) >= 2:
                            val1 = float(values[0])
                            val2 = float(values[1])

                            # Get current time
                            current_time = time.time()
                            if self.start_time is None:
                                self.start_time = current_time

                            # Calculate relative time in seconds
                            relative_time = current_time - self.start_time

                            # Add to buffers
                            self.timestamps.append(relative_time)
                            self.data1.append(val1)
                            self.data2.append(val2)
                            self.sample_count += 1
                            self.good_samples += 1

                            # Print first successful parse
                            if self.good_samples == 1:
                                print(f"First valid data - Charge: {val1}, Discharge: {val2}", flush=True)

                            # Trim old data outside the time window
                            cutoff_time = relative_time - self.window_seconds
                            while len(self.timestamps) > 0 and self.timestamps[0] < cutoff_time:
                                self.timestamps.pop(0)
                                self.data1.pop(0)
                                self.data2.pop(0)

                    except ValueError as e:
                        self.bad_samples += 1
                        # Only print first few errors
                        if self.bad_samples <= 10:
                            print(f"Error parsing line '{line}': {e}")
                        elif self.bad_samples == 11:
                            print(f"Suppressing further parse errors... (Good: {self.good_samples}, Bad: {self.bad_samples})")

            # Update plots if we have data
            if len(self.data1) > 0:
                # Convert timestamps and data to numpy arrays
                x = np.array(self.timestamps)
                y1 = np.array(self.data1)
                y2 = np.array(self.data2)

                # Update curves
                self.curve1.setData(x, y1)
                self.curve2.setData(x, y2)

                # Set fixed time window - scroll as new data comes in
                current_time = x[-1]
                x_min = max(0, current_time - self.window_seconds)
                x_max = max(self.window_seconds, current_time)

                self.p1.setXRange(x_min, x_max, padding=0)

                # Manually set Y-ranges with padding and minimum scale
                y1_min, y1_max = np.min(y1), np.max(y1)
                y1_range = y1_max - y1_min
                y1_range = max(y1_range, 0.1)  # Ensure minimum range of 0.1 µs
                y1_center = (y1_min + y1_max) / 2
                self.p1.setYRange(y1_center - 0.55 * y1_range, y1_center + 0.55 * y1_range, padding=0)

                y2_min, y2_max = np.min(y2), np.max(y2)
                y2_range = y2_max - y2_min
                y2_range = max(y2_range, 0.1)  # Ensure minimum range of 0.1 µs
                y2_center = (y2_min + y2_max) / 2
                self.p2.setYRange(y2_center - 0.55 * y2_range, y2_center + 0.55 * y2_range, padding=0)

                # Update title with current values
                latest_charge = y1[-1]
                latest_discharge = y2[-1]
                title_str = f'<span style="color: #00ff00; font-size: 18pt;">Charge: {latest_charge:.3f} µs</span>  '
                title_str += f'<span style="color: #ff0000; font-size: 18pt;">Discharge: {latest_discharge:.3f} µs</span>'
                self.p1.setTitle(title_str)

        except Exception as e:
            print(f"Error updating plot: {e}")

    def cleanup(self):
        """Close serial port."""
        if self.serial.is_open:
            self.serial.close()
            print("Serial port closed")

def main():
    parser = OptionParser(usage="usage: %prog [options]")
    parser.add_option("-p", "--port", dest="port",
                      default="/dev/cu.usbmodem1402",
                      help="Serial port path [default: %default]")
    parser.add_option("-b", "--baudrate", dest="baudrate",
                      type="int", default=115200,
                      help="Baud rate [default: %default]")
    parser.add_option("-w", "--window", dest="window_seconds",
                      type="int", default=10,
                      help="Time window in seconds [default: %default]")

    (options, args) = parser.parse_args()

    # Create Qt Application
    app = QApplication(sys.argv)

    # Create plotter
    plotter = SerialPlotter(options.port, options.baudrate, options.window_seconds)

    # Handle cleanup on exit
    app.aboutToQuit.connect(plotter.cleanup)

    # Run
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
