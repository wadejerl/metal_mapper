#!/usr/bin/env python3
"""
Test data generator for map_serial_stream.py
Simulates a device moving around and sending lat,lon,value data
Uses a virtual serial port or can be piped directly
"""
import sys
import time
import random
import math
from optparse import OptionParser

def generate_test_track(start_lat=37.7749, start_lon=-122.4194, num_points=100,
                       step_size=0.0001, value_min=2.5, value_max=4.0):
    """
    Generate a test track with simulated GPS coordinates and values.

    Args:
        start_lat: Starting latitude
        start_lon: Starting longitude
        num_points: Number of points to generate (0 for infinite)
        step_size: Size of movement steps in degrees (~11m at equator)
        value_min: Minimum value
        value_max: Maximum value
    """
    lat = start_lat
    lon = start_lon
    angle = 0  # Direction of movement

    count = 0
    while True:
        # Generate value with some variation
        value = random.uniform(value_min, value_max)

        # Add some noise to simulate real sensor readings
        value_noise = random.gauss(0, (value_max - value_min) * 0.05)
        value = max(value_min, min(value_max, value + value_noise))

        # Output data with timestamp
        timestamp = time.time()
        print(f"{lat:.8f},{lon:.8f},{value:.3f},{timestamp:.3f}", flush=True)

        # Update position (random walk with some momentum)
        angle += random.gauss(0, 30)  # Change direction
        lat += step_size * math.sin(math.radians(angle))
        lon += step_size * math.cos(math.radians(angle))

        # Keep within reasonable bounds
        lat = max(start_lat - 0.01, min(start_lat + 0.01, lat))
        lon = max(start_lon - 0.01, min(start_lon + 0.01, lon))

        count += 1
        if num_points > 0 and count >= num_points:
            break

        time.sleep(0.05)  # 20 Hz update rate

def main():
    parser = OptionParser(usage="usage: %prog [options]")
    parser.add_option("--lat", dest="start_lat",
                      type="float", default=37.7749,
                      help="Starting latitude [default: %default]")
    parser.add_option("--lon", dest="start_lon",
                      type="float", default=-122.4194,
                      help="Starting longitude [default: %default]")
    parser.add_option("-n", "--num-points", dest="num_points",
                      type="int", default=0,
                      help="Number of points (0=infinite) [default: %default]")
    parser.add_option("-s", "--step-size", dest="step_size",
                      type="float", default=0.000001,
                      help="Movement step size in degrees [default: %default]")
    parser.add_option("--value-min", dest="value_min",
                      type="float", default=2.5,
                      help="Minimum value [default: %default]")
    parser.add_option("--value-max", dest="value_max",
                      type="float", default=4.0,
                      help="Maximum value [default: %default]")

    (options, args) = parser.parse_args()

    print(f"Generating test data starting at ({options.start_lat}, {options.start_lon})",
          file=sys.stderr)
    print(f"Value range: {options.value_min} - {options.value_max}", file=sys.stderr)
    print(f"Press Ctrl+C to stop\n", file=sys.stderr)

    try:
        generate_test_track(
            options.start_lat,
            options.start_lon,
            options.num_points,
            options.step_size,
            options.value_min,
            options.value_max
        )
    except KeyboardInterrupt:
        print("\nStopped", file=sys.stderr)

if __name__ == '__main__':
    main()
