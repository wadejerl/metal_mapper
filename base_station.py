#!/usr/bin/env python3
"""
Base Station Configuration Tool- Mostly written by Claude-code based on my base_stn.py

This script configures a GNSS receiver (Quectel) to operate as a base station
with RTCM output and Survey-In mode.  
"""

import sys
import os
import subprocess
import time
from serial import Serial
from pynmeagps import NMEAReader, NMEAMessage, GET, SET, POLL


class BaseStation:
    """
    Manages configuration and communication with a GNSS base station receiver.
    """

    def _get_cold_reset_msgs(self):
        """Generate configuration messages with current settings."""
        return [
            NMEAMessage('P', 'QTMCFGRCVRMODE', SET, payload=['W', '2']),
            NMEAMessage('P', 'QTMCFGRTCM', SET, payload=['W', '7', '0', '-90', '07', '06', '1', '0']),
            NMEAMessage('P', 'QTMCFGSVIN', SET, payload=['W', '1', self.svin_duration, '1.5', '0.0', '0.0', '0.0']),
            NMEAMessage('P', 'QTMSAVEPAR', SET),
        ]

    POST_RESET_MSGS = [
        NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', 'PQTMSVINSTATUS', '1', '1']),
        NMEAMessage('P', 'QTMSAVEPAR', SET),
    ]

    def __init__(self, port, baudrate=460800, timeout=3, wait_for_svin=False, svin_duration=5):
        """
        Initialize the BaseStation controller.

        Args:
            port (str): Serial port path (e.g., '/dev/cu.usbmodem59320022501')
            baudrate (int): Baud rate for serial communication (default: 460800)
            timeout (int): Serial port timeout in seconds (default: 3)
            wait_for_svin (bool): Wait for survey-in to complete before exiting (default: False)
            svin_duration (int): Survey-in duration in seconds (default: 5)
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.wait_for_svin = wait_for_svin
        self.svin_duration = str(svin_duration)
        self.stream = None
        self.nmr = None

    def connect(self):
        """
        Establish serial connection to the base station.

        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            self.stream = Serial(self.port, self.baudrate, timeout=self.timeout)
            self.nmr = NMEAReader(self.stream)
            print(f"Connected to {self.port} at {self.baudrate} baud")
            return True
        except Exception as e:
            print(f"Error connecting to {self.port}: {e}")
            return False

    def disconnect(self):
        """Close the serial connection."""
        if self.stream and self.stream.is_open:
            self.stream.close()
            print("Disconnected from base station")

    def send_messages(self, messages):
        """
        Send a list of NMEA messages to the receiver.

        Args:
            messages (list): List of NMEAMessage objects to send
        """
        for msg in messages:
            print(f"Sending message {msg}: ", end="")
            serialized = msg.serialize()
            print(serialized)
            self.stream.write(serialized)
            self.stream.flush()

    def send_reset(self):
        """Send a reset command and wait for the receiver to restart."""
        print("Sending reset command...")
        self.stream.write(NMEAMessage('P', 'QTMSRR', SET).serialize())
        self.stream.flush()
        time.sleep(3)

    def send_factory_reset(self):
        """Send a factory reset command and wait for completion."""
        print("Sending factory reset command...")
        self.stream.write(NMEAMessage('P', 'QTMRESTOREPAR', SET).serialize())
        self.stream.flush()
        time.sleep(1)

    def configure(self):
        """
        Execute the full configuration sequence for the base station.

        This includes factory reset, configuration, and initialization of survey-in mode.
        """
        print("Starting configuration sequence...")

        # Factory reset and reboot
        self.send_factory_reset()
        self.send_reset()

        # Send initial configuration
        sys.stdout.flush()
        print("Applying cold reset configuration...")
        self.send_messages(self._get_cold_reset_msgs())

        # Reboot to apply settings
        self.send_reset()

        # Send post-reset configuration
        print("Applying post-reset configuration...")
        self.send_messages(self.POST_RESET_MSGS)

        # Poll survey-in status
        print("Polling survey-in configuration...")
        self.stream.write(NMEAMessage('P', 'QTMCFGSVIN', POLL).serialize())

        print("Configuration complete!")

    def monitor(self):
        """
        Monitor and display incoming NMEA messages from the receiver.

        This runs in an infinite loop until interrupted, or until survey-in
        completes if wait_for_svin is enabled.
        """
        if self.wait_for_svin:
            print("Waiting for survey-in to complete (watching for valid=2)...")
        else:
            print("Monitoring receiver output (Ctrl+C to stop)...")

        try:
            while True:
                raw_data, parsed_data = self.nmr.read()
                if parsed_data is not None:
                    print(parsed_data)

                    # Check if this is a PQTMSVINSTATUS message and if survey-in is complete
                    if self.wait_for_svin and hasattr(parsed_data, 'msgID'):
                        if parsed_data.msgID == 'QTMSVINSTATUS' and hasattr(parsed_data, 'valid'):
                            if parsed_data.valid == 2:
                                print("\nSurvey-in complete! (valid=2)")
                                return
        except KeyboardInterrupt:
            print("\nMonitoring stopped by user")

    def run(self):
        """
        Execute the complete base station setup and monitoring sequence.
        """
        if not self.connect():
            return False

        try:
            self.configure()
            self.monitor()
        finally:
            self.disconnect()

        return True


if __name__ == "__main__":
    from optparse import OptionParser

    parser = OptionParser(
        usage="usage: %prog [options]",
        description="Configure and monitor a GNSS base station receiver"
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

    parser.add_option(
        "-w", "--wait",
        dest="wait_for_svin",
        action="store_true",
        default=False,
        help="Wait for survey-in to complete (valid=2) before exiting"
    )

    parser.add_option(
        "-d", "--duration",
        dest="svin_duration",
        type="int",
        default=5,
        help="Survey-in duration in seconds (default: 5)",
        metavar="DURATION"
    )

    (options, args) = parser.parse_args()

    # Create and run base station
    base_station = BaseStation(options.port, timeout=options.timeout, wait_for_svin=options.wait_for_svin, svin_duration=options.svin_duration)

    try:
        base_station.run()
        base_station.disconnect()

        # Launch gnssserver from venv
        print("\nStarting GNSS server...")
        gnssserver_path = os.path.join(sys.prefix, 'bin', 'gnssserver')
        subprocess.run([
            gnssserver_path,
            '--inport', options.port,
            '--baudrate', '460800',
            '--outport', '2101',
            '--ntripmode', '1',
            '--protfilter', '4',
            '--format', '2',
            '--ntripuser', 'anon',
            '--ntrippassword', 'password',
            '--verbosity', '2'
        ])
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
