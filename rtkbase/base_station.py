#!/usr/bin/env python3
"""
Base Station Configuration Tool — mostly written by Claude Code from an earlier
hand-written survey-in script (retired August 2026; it survives in the v0.1 tag).

This script configures a GNSS receiver (Quectel) to operate as a base station
with RTCM output and Survey-In mode.  
"""

import sys
import os
import subprocess
import time
import datetime
import shutil
from serial import Serial
from pynmeagps import NMEAReader, NMEAMessage, GET, SET, POLL

# One-shot GPS->system-clock sync only makes sense as root on Linux;
# bench runs on macOS must never touch the host clock.
CAN_SET_CLOCK = (sys.platform == 'linux' and os.geteuid() == 0
                 and hasattr(time, 'clock_settime'))


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
        # Base mode disables all standard NMEA; RMC is the clock-sync time
        # source (rovers never see it: gnssserver forwards RTCM only).
        NMEAMessage('P', 'QTMCFGMSGRATE', SET, payload=['W', 'RMC', '1']),
        NMEAMessage('P', 'QTMSAVEPAR', SET),
    ]

    def __init__(self, port, baudrate=460800, timeout=3, wait_for_svin=False, svin_duration=5):
        """
        Initialize the BaseStation controller.

        Args:
            port (str): Serial port path (e.g., '/dev/ttyACM0')
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
        self.clock_synced = False

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

    def _maybe_set_clock(self, msg):
        """
        One-shot: step the system clock from the first valid GNSS RMC.

        Any failure disarms the hook for the rest of the run — a crash here
        under Restart=always would loop factory-reset forever and the NTRIP
        caster would never come up.
        """
        if self.clock_synced or not CAN_SET_CLOCK:
            return
        try:
            if getattr(msg, 'msgID', None) != 'RMC' or msg.status != 'A':
                return
            if not isinstance(msg.date, datetime.date) or not isinstance(msg.time, datetime.time):
                return  # no-fix RMC carries empty strings for date/time
            gps = datetime.datetime.combine(msg.date, msg.time,
                                            tzinfo=datetime.timezone.utc)
            delta = gps.timestamp() - time.time()
            if abs(delta) > 2.0:
                time.clock_settime(time.CLOCK_REALTIME, gps.timestamp())
                print(f"System clock stepped {delta:+.1f} s to GPS time {gps.isoformat()}")
            else:
                print(f"System clock already within {delta:+.1f} s of GPS time")
            self.clock_synced = True
        except Exception as e:
            print(f"Clock set failed ({e}); giving up on clock sync")
            self.clock_synced = True

    def _drain_for_clock(self, max_wait=15):
        """
        Give the clock sync a last bounded chance before monitor() returns
        (run() closes the port right after). Deadline on monotonic: the hook
        steps CLOCK_REALTIME out from under a wall-clock deadline.
        """
        if self.clock_synced or not CAN_SET_CLOCK:
            return
        print(f"Waiting up to {max_wait} s for a valid RMC to set the clock...")
        deadline = time.monotonic() + max_wait
        while not self.clock_synced and time.monotonic() < deadline:
            raw_data, parsed_data = self.nmr.read()
            if parsed_data is not None:
                self._maybe_set_clock(parsed_data)
        if not self.clock_synced:
            print("No valid RMC seen; continuing without clock sync")

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
                    self._maybe_set_clock(parsed_data)

                    # Check if this is a PQTMSVINSTATUS message and if survey-in is complete
                    if self.wait_for_svin and hasattr(parsed_data, 'msgID'):
                        if parsed_data.msgID == 'QTMSVINSTATUS' and hasattr(parsed_data, 'valid'):
                            if parsed_data.valid == 2:
                                print("\nSurvey-in complete! (valid=2)")
                                self._drain_for_clock()
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


def persist_clock():
    """
    Best-effort follow-ups once the clock is GPS-correct: write it to the
    battery RTC (if any) and to fake-hwclock's file so it survives an
    unclean power cut, then let chrony serve the LAN even while offline.
    'local' is deliberately NOT in chrony.conf — a static entry would start
    serving the stale pre-step time the moment chronyd boots. hwclock exits
    nonzero when no RTC exists — that is fine.
    """
    fake_hwclock = shutil.which('fake-hwclock') or 'fake-hwclock'
    chronyc = shutil.which('chronyc') or '/usr/bin/chronyc'
    for cmd in (['/usr/sbin/hwclock', '--systohc'],
                [fake_hwclock, 'save'],
                [chronyc, 'local', 'stratum', '10']):
        try:
            subprocess.run(cmd, timeout=10)
        except Exception as e:
            print(f"{cmd[0]}: {e} (ignored)")


if __name__ == "__main__":
    from optparse import OptionParser

    parser = OptionParser(
        usage="usage: %prog [options]",
        description="Configure and monitor a GNSS base station receiver"
    )

    parser.add_option(
        "-p", "--port",
        dest="port",
        default="/dev/ttyACM0",
        help="Serial port path (default: /dev/ttyACM0)",
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

    if not CAN_SET_CLOCK:
        print("GPS clock sync disabled (requires Linux + root)")

    # Create and run base station
    base_station = BaseStation(options.port, timeout=options.timeout, wait_for_svin=options.wait_for_svin, svin_duration=options.svin_duration)

    try:
        if not base_station.run():
            # Port not openable (USB still enumerating, transient EBUSY).
            # Exit so systemd's 2 s restart loop retries — falling through
            # would let gnssserver capture the port with the receiver
            # unconfigured and the clock never stepped.
            sys.exit(1)
        base_station.disconnect()

        # Persist the GPS-stepped time before the caster takes over
        if CAN_SET_CLOCK and base_station.clock_synced:
            persist_clock()

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
            '--maxclients', '10',
            '--ntripuser', 'anon',
            '--ntrippassword', 'password',
            '--verbosity', '1'
        ])
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
