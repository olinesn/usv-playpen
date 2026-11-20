"""
@author: olinesn
Logs the sync Arduino's serial output to a capture file, in place of CoolTerm off Windows.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
import threading
from typing import IO

import serial


def resolve_sync_logger(sync_logger_setting: str,
                        operating_system: str) -> str:
    """
    Description
    -----------
    Decides which program logs the sync Arduino's serial output during a recording.

    ``'auto'`` keeps CoolTerm on Windows, as before, and picks the built-in logger
    (:class:`SerialSyncLogger`) on every other system, where usv-playpen does not
    start CoolTerm. ``'coolterm'`` and ``'builtin'`` force one of the two;
    ``'coolterm'`` is refused off Windows, because only the Windows launch
    (PowerShell ``Start-Process`` on the ``.stc`` settings file) is implemented.

    Parameters
    ----------
    sync_logger_setting (str)
        The ``arduino_sync_logger`` setting: ``'auto'``, ``'coolterm'`` or ``'builtin'``.
    operating_system (str)
        The system name as ``platform.system()`` returns it (``'Windows'``,
        ``'Linux'``, ``'Darwin'``).

    Returns
    -------
    sync_logger (str)
        ``'coolterm'`` or ``'builtin'``.
    """

    if sync_logger_setting not in ('auto', 'coolterm', 'builtin'):
        msg = (f"arduino_sync_logger must be 'auto', 'coolterm' or 'builtin', "
               f"got {sync_logger_setting!r}.")
        raise ValueError(msg)

    if sync_logger_setting == 'auto':
        return 'coolterm' if operating_system == 'Windows' else 'builtin'

    if sync_logger_setting == 'coolterm' and operating_system != 'Windows':
        msg = (f"arduino_sync_logger = 'coolterm' works on Windows only (this is {operating_system}); "
               f"set it to 'auto' or 'builtin' to log the sync pulses with usv-playpen itself.")
        raise ValueError(msg)

    return sync_logger_setting


class SerialSyncLogger:
    """
    Description
    -----------
    Writes every byte the sync Arduino sends over its serial port into one
    capture file, unchanged, as CoolTerm does with the lab's
    ``coolterm_config.stc`` (raw capture, 8 data bits, no parity, 1 stop bit).
    The sketch ``generate_sync_pulses.ino`` prints each inter-pulse interval
    (ms) as one ``\\r\\n``-terminated line, so the file holds one interval per
    line, which ``processing/synchronize_files.py`` reads as the ground-truth
    pulse train.

    The file is named the way CoolTerm names its auto-capture files: the
    ``arduino_sync_capture_prefix`` setting, then the local time of the
    capture start, ``YYYY-MM-DD HH-MM-SS-mmm``, and ``.txt``. The shipped
    prefix, ``CoolTerm Capture (usv-playpen)``, has ``usv-playpen`` where
    CoolTerm puts the name of its settings file. The ``CoolTerm`` in the name
    is what ``synchronize_files.py`` looks for, so sessions recorded with
    either program are processed alike.

    The port is read on a background thread, and each chunk is flushed to disk
    as it arrives. If the port goes away during the recording (the Arduino is
    unplugged or resets its USB link), the thread keeps trying to reopen it and
    appends to the same file once it is back, as CoolTerm's
    ``AutoReconnect = true`` does; each loss and reconnection is listed in
    ``events``. The thread never calls ``message_output``: Qt widgets must not
    be touched from it, so the caller reports ``events`` after
    :meth:`stop_capture`.
    """

    def __init__(self, port: str,
                 baud_rate: int,
                 capture_directory: str | pathlib.Path,
                 capture_prefix: str,
                 read_timeout_s: float) -> None:
        """
        Description
        -----------
        Initializes the SerialSyncLogger class. Nothing is opened until
        :meth:`start_capture`.

        Parameters
        ----------
        port (str)
            The serial port of the sync Arduino, as the operating system names
            it: ``COM7`` on Windows, ``/dev/ttyACM0`` on Linux,
            ``/dev/cu.usbmodem1101`` on macOS. A pyserial URL (e.g.
            ``loop://``) works too.
        baud_rate (int)
            The baud rate the sketch opens its serial port with (9600 in
            ``generate_sync_pulses.ino``).
        capture_directory (str | pathlib.Path)
            Absolute path of the local directory the capture file is written
            to; it is created if missing.
        capture_prefix (str)
            The start of the capture file's name (``arduino_sync_capture_prefix``);
            the capture's start time and ``.txt`` are appended to it, as CoolTerm
            does. ``synchronize_files.py`` finds the capture by the word
            ``CoolTerm`` in its name.
        read_timeout_s (float)
            How long (s) one read of the port waits for data. It bounds how
            long :meth:`stop_capture` waits for the thread, and is the pause
            between attempts at reopening a lost port.

        Returns
        -------
        None
        """

        self.port = port
        self.baud_rate = baud_rate
        self.capture_directory = pathlib.Path(capture_directory)
        self.capture_prefix = capture_prefix
        self.read_timeout_s = read_timeout_s
        self.capture_file: pathlib.Path | None = None
        self.bytes_written = 0
        self.events: list[str] = []
        self._serial_port: serial.SerialBase | None = None
        self._capture_handle: IO[bytes] | None = None
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

    def _open_port(self) -> serial.SerialBase:
        """
        Description
        -----------
        Opens the Arduino's serial port at ``baud_rate``, 8N1, with
        ``read_timeout_s`` as the read timeout. On Linux and macOS the port is
        opened for exclusive use, so a second program (a serial monitor, a
        second logger) cannot open it and take part of the bytes.

        Opening the port sets DTR, which restarts an Arduino Uno, exactly as
        CoolTerm's ``DTRDefaultState = true`` does: the pulse train pauses for
        about two seconds and resumes with a new random seed.

        Parameters
        ----------

        Returns
        -------
        serial_port (serial.SerialBase)
            The open port.
        """

        return serial.serial_for_url(self.port,
                                     baudrate=self.baud_rate,
                                     bytesize=serial.EIGHTBITS,
                                     parity=serial.PARITY_NONE,
                                     stopbits=serial.STOPBITS_ONE,
                                     timeout=self.read_timeout_s,
                                     exclusive=True)

    def start_capture(self) -> pathlib.Path:
        """
        Description
        -----------
        Opens the Arduino's serial port, creates the capture file, and starts
        the thread that copies the port into the file. Fails before anything is
        recorded if the port cannot be opened, so a wrong ``arduino_sync_port``
        stops the recording at its start instead of leaving it without sync
        pulses.

        Parameters
        ----------

        Returns
        -------
        capture_file (pathlib.Path)
            Path of the capture file being written.
        """

        if self._reader_thread is not None:
            msg = f"The sync capture on {self.port} has already been started."
            raise RuntimeError(msg)

        if not self.capture_directory.is_absolute():
            msg = (f"The sync capture directory must be an absolute path on this computer, "
                   f"got '{self.capture_directory}' (set coolterm_basedirectory).")
            raise ValueError(msg)

        try:
            self._serial_port = self._open_port()
        except (serial.SerialException, OSError, ValueError) as error:
            msg = (f"Could not open the sync Arduino's serial port '{self.port}' "
                   f"(arduino_sync_port): {error}")
            raise RuntimeError(msg) from error

        capture_start = dt.datetime.now()
        capture_stamp = f"{capture_start:%Y-%m-%d %H-%M-%S}-{capture_start.microsecond // 1000:03d}"
        self.capture_directory.mkdir(parents=True, exist_ok=True)
        self.capture_file = self.capture_directory / f"{self.capture_prefix} {capture_stamp}.txt"
        self._capture_handle = self.capture_file.open('xb')

        self._reader_thread = threading.Thread(target=self._capture_loop,
                                               name='sync-serial-logger',
                                               daemon=True)
        self._reader_thread.start()

        return self.capture_file

    def _capture_loop(self) -> None:
        """
        Description
        -----------
        Runs on the background thread until :meth:`stop_capture` is called:
        reads whatever the port holds (waiting at most ``read_timeout_s`` for
        the first byte), writes it to the capture file and flushes it. When the
        port fails, it is closed and reopened every ``read_timeout_s`` until it
        answers again or the capture is stopped.

        Parameters
        ----------

        Returns
        -------
        None
        """

        while not self._stop_event.is_set():
            if self._serial_port is None:
                try:
                    self._serial_port = self._open_port()
                except (serial.SerialException, OSError):
                    self._stop_event.wait(self.read_timeout_s)
                    continue
                self.events.append(f"{dt.datetime.now():%H:%M:%S} reopened {self.port}")

            try:
                chunk = self._serial_port.read(self._serial_port.in_waiting or 1)
            except (serial.SerialException, OSError) as error:
                self.events.append(f"{dt.datetime.now():%H:%M:%S} lost {self.port}: {error}")
                with contextlib.suppress(serial.SerialException, OSError):
                    self._serial_port.close()
                self._serial_port = None
                continue

            if chunk and self._capture_handle is not None:
                self._capture_handle.write(chunk)
                self._capture_handle.flush()
                self.bytes_written += len(chunk)

    def stop_capture(self) -> int:
        """
        Description
        -----------
        Stops the background thread (within one ``read_timeout_s``), then
        closes the serial port and the capture file. Calling it again, or
        before :meth:`start_capture`, does nothing.

        Parameters
        ----------

        Returns
        -------
        bytes_written (int)
            Number of bytes written to the capture file.
        """

        if self._reader_thread is None:
            return self.bytes_written

        self._stop_event.set()
        self._reader_thread.join()
        self._reader_thread = None

        if self._serial_port is not None:
            with contextlib.suppress(serial.SerialException, OSError):
                self._serial_port.close()
            self._serial_port = None

        if self._capture_handle is not None:
            self._capture_handle.close()
            self._capture_handle = None

        return self.bytes_written
