#!/usr/bin/env python3
"""
Rover Configuration Tool- for the normal 2-GPS setup, this will run in 2 instances

This script configures a GNSS receiver (Quectel) to operate as a rover.
"""

import sys
import os
import subprocess
from base_station import BaseStation
from pynmeagps import NMEAReader, NMEAMessage, GET, SET, POLL

# Rover-specific configuration messages
ROVER_CONFIG_MSGS = [
    NMEAMessage('P', 'QTMCFGFIXRATE', SET, payload=['W', '50']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '2', 'GGA', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '2', 'GSV', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '2', 'GSA', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '2', 'VTG', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '2', 'GLL', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '2', 'RMC', '0']),

    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '1', 'GGA', '1']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '1', 'GSV', '1']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '1', 'GSA', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '1', 'VTG', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '1', 'GLL', '0']),
    NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', '1', '1', 'RMC', '0']),
    NMEAMessage('P', 'QTMSAVEPAR', SET),
]


if __name__ == "__main__":
    from optparse import OptionParser

    parser = OptionParser(
        usage="usage: %prog [options]",
        description="Configure a GNSS rover receiver"
    )

    parser.add_option(
        "-p", "--port",
        dest="port",
        default="/dev/cu.usbmodem59320022501",
        help="Serial port path (default: /dev/cu.usbmodem59320022501)",
        metavar="PORT"
    )

    parser.add_option(
        "-t", "--timeout",
        dest="timeout",
        type="int",
        default=3,
        help="Serial timeout in seconds (default: 3)",
        metavar="TIMEOUT"
    )

    (options, args) = parser.parse_args()

    # Create base station instance
    rover = BaseStation(options.port, timeout=options.timeout)

    try:
        # Connect and send rover-specific commands
        if rover.connect():
            rover.send_factory_reset()
            rover.send_reset()
            rover.send_messages(ROVER_CONFIG_MSGS)
            rover.send_reset()
            rover.monitor()     # blocks forever
            #rover.send_reset()
            rover.disconnect()

            # Launch external subprocess
            print("\nStarting external process...")
            #external_cmd = os.path.join(sys.prefix, 'bin', 'your_command_here')
            #subprocess.run([
            #    external_cmd,
            #    '--port', options.port,
            #    '--baudrate', '460800',
                # Add other arguments as needed
            #])

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
