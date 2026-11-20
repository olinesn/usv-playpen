"""
@author: olinesn
Test module: usv_playpen.recording.sync_serial_logger, the built-in logger of the
sync Arduino's serial output, with no Arduino attached -- the port is a
pseudo-terminal pair (a fake Arduino writes into one end, the logger opens the
other), pyserial's ``loop://`` port, or a stand-in port object.

Coverage: ``resolve_sync_logger`` (each setting on each operating system, a
misspelled setting); ``SerialSyncLogger`` (byte-for-byte copies of real CoolTerm
captures, the CoolTerm-style file name and its prefix setting, an empty capture, exclusive use of the
port, reconnecting after the port is lost, a port that cannot be opened, a
relative capture directory, starting twice, stopping twice).
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Iterator

import pytest
import serial

from usv_playpen.recording import sync_serial_logger as ssl_mod
from usv_playpen.recording.sync_serial_logger import (
    SerialSyncLogger,
    resolve_sync_logger,
)

# The first bytes of two real CoolTerm captures (falkner/Bartul/Data, sessions
# 20240307_160336 and 20260421_182605). The first started mid-line, as about a
# third of the lab's captures do; synchronize_files.py skips the first three
# lines for this reason.
REAL_CAPTURE_PARTIAL_START = b'\n2\r\n557\r\n343\r\n423\r\n273\r\n1361\r\n492\r\n1296\r\n378\r\n'
REAL_CAPTURE_CLEAN_START = b'1223\r\n805\r\n1139\r\n509\r\n1218\r\n880\r\n1194\r\n962\r\n'

CAPTURE_NAME_PATTERN = re.compile(r'CoolTerm Capture \(usv-playpen\) \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}-\d{3}\.txt')


def _wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """
    Description
    -----------
    Polls ``condition`` every 10 ms until it holds, failing the test after
    ``timeout_s``.

    Parameters
    ----------
    condition (Callable[[], bool])
        The condition to wait for.
    timeout_s (float)
        How long to wait at most.

    Returns
    -------
    None
    """

    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"condition not met within {timeout_s} s")
        time.sleep(0.01)


@pytest.fixture
def fake_arduino_port() -> Iterator[tuple[int, str]]:
    """
    Description
    -----------
    A pseudo-terminal pair standing in for the Arduino's USB serial port:
    bytes written to the first end arrive at the device path of the second,
    which the logger opens like ``/dev/ttyACM0``. The second end is also held
    open here, so the pair outlives the logger closing it.

    Parameters
    ----------

    Returns
    -------
    (arduino_fd, port_path) (tuple[int, str])
        The file descriptor the fake Arduino writes to, and the port path.
    """

    arduino_fd, port_fd = os.openpty()
    yield arduino_fd, os.ttyname(port_fd)
    os.close(arduino_fd)
    os.close(port_fd)


class _StandInPort:
    """
    Description
    -----------
    A serial port stand-in that hands out the given chunks, one per read, and
    then raises ``serial.SerialException`` (an unplugged Arduino) or, without
    ``fail_after``, returns nothing.
    """

    def __init__(self, chunks: list[bytes], fail_after: bool) -> None:
        """
        Description
        -----------
        Stores the chunks to hand out.

        Parameters
        ----------
        chunks (list[bytes])
            The chunks, in order.
        fail_after (bool)
            Whether to raise once the chunks are used up.

        Returns
        -------
        None
        """

        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.closed = False

    @property
    def in_waiting(self) -> int:
        """Bytes ready to read: the next chunk's length."""
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, _size: int) -> bytes:
        """The next chunk, then a failure or nothing (after a short wait, as a read timeout)."""
        if self.chunks:
            return self.chunks.pop(0)
        if self.fail_after:
            msg = 'device reports readiness to read but returned no data'
            raise serial.SerialException(msg)
        time.sleep(0.01)
        return b''

    def close(self) -> None:
        """Marks the port closed."""
        self.closed = True


class TestResolveSyncLogger:

    @pytest.mark.parametrize(('setting', 'operating_system', 'expected'), [
        ('auto', 'Windows', 'coolterm'),
        ('auto', 'Linux', 'builtin'),
        ('auto', 'Darwin', 'builtin'),
        ('builtin', 'Windows', 'builtin'),
        ('builtin', 'Linux', 'builtin'),
        ('coolterm', 'Windows', 'coolterm'),
    ])
    def test_resolve_sync_logger_picks_the_program(self, setting, operating_system, expected):
        """'auto' keeps CoolTerm on Windows only; the forced settings are taken as they are."""
        assert resolve_sync_logger(sync_logger_setting=setting, operating_system=operating_system) == expected

    @pytest.mark.parametrize('operating_system', ['Linux', 'Darwin'])
    def test_resolve_sync_logger_refuses_coolterm_off_windows(self, operating_system):
        """Only the Windows CoolTerm launch exists, so asking for CoolTerm elsewhere fails loud."""
        with pytest.raises(ValueError, match='Windows only'):
            resolve_sync_logger(sync_logger_setting='coolterm', operating_system=operating_system)

    def test_resolve_sync_logger_refuses_unknown_setting(self):
        """A misspelled setting is named in the error instead of silently picking a logger."""
        with pytest.raises(ValueError, match="'CoolTerm'"):
            resolve_sync_logger(sync_logger_setting='CoolTerm', operating_system='Windows')


@pytest.mark.skipif(sys.platform == 'win32', reason='pseudo-terminals are POSIX-only')
class TestCaptureFromFakeArduino:

    def test_capture_matches_sent_bytes_exactly(self, fake_arduino_port, tmp_path):
        """The capture holds exactly the bytes the Arduino sent, CRLFs and a partial first line included, as CoolTerm's raw capture does."""
        arduino_fd, port_path = fake_arduino_port
        sent = REAL_CAPTURE_PARTIAL_START + REAL_CAPTURE_CLEAN_START
        logger = SerialSyncLogger(port=port_path, baud_rate=9600, capture_directory=tmp_path / 'Data', capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.05)
        capture_file = logger.start_capture()
        # chunk borders fall inside numbers and inside CRLFs on purpose
        for chunk_start in range(0, len(sent), 7):
            os.write(arduino_fd, sent[chunk_start:chunk_start + 7])
        _wait_until(lambda: logger.bytes_written == len(sent))
        assert logger.stop_capture() == len(sent)
        assert capture_file.read_bytes() == sent
        assert capture_file.parent == tmp_path / 'Data'
        assert CAPTURE_NAME_PATTERN.fullmatch(capture_file.name)
        assert logger.events == []

    def test_capture_is_flushed_while_recording(self, fake_arduino_port, tmp_path):
        """Every line reaches the disk as it arrives, so a crash mid-recording loses nothing already received."""
        arduino_fd, port_path = fake_arduino_port
        logger = SerialSyncLogger(port=port_path, baud_rate=9600, capture_directory=tmp_path, capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.05)
        capture_file = logger.start_capture()
        os.write(arduino_fd, REAL_CAPTURE_CLEAN_START)
        _wait_until(lambda: capture_file.read_bytes() == REAL_CAPTURE_CLEAN_START)
        logger.stop_capture()

    def test_second_logger_cannot_share_the_port(self, fake_arduino_port, tmp_path):
        """Two readers of one port would each get part of the bytes, so a second open is refused."""
        _arduino_fd, port_path = fake_arduino_port
        first = SerialSyncLogger(port=port_path, baud_rate=9600, capture_directory=tmp_path / 'a', capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.05)
        first.start_capture()
        second = SerialSyncLogger(port=port_path, baud_rate=9600, capture_directory=tmp_path / 'b', capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.05)
        with pytest.raises(RuntimeError, match='arduino_sync_port'):
            second.start_capture()
        first.stop_capture()


class TestSerialSyncLogger:

    def test_capture_without_data_leaves_empty_file(self, tmp_path):
        """A silent Arduino yields an empty capture file and zero bytes, which the recording reports."""
        logger = SerialSyncLogger(port='loop://', baud_rate=9600, capture_directory=tmp_path, capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.02)
        capture_file = logger.start_capture()
        assert logger.stop_capture() == 0
        assert capture_file.read_bytes() == b''

    def test_capture_file_name_starts_with_the_prefix_setting(self, tmp_path):
        """The capture is named from arduino_sync_capture_prefix, so a rig can tell its captures apart while processing still finds 'CoolTerm'."""
        logger = SerialSyncLogger(port='loop://', baud_rate=9600, capture_directory=tmp_path, capture_prefix='CoolTerm Capture (rig B)', read_timeout_s=0.02)
        capture_file = logger.start_capture()
        logger.stop_capture()
        assert re.fullmatch(r'CoolTerm Capture \(rig B\) \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}-\d{3}\.txt', capture_file.name)

    def test_capture_reopens_lost_port_and_appends(self, monkeypatch, tmp_path):
        """An unplugged-and-replugged Arduino keeps writing into the same file, as CoolTerm's AutoReconnect does, and the gap is listed."""
        ports_to_open: list[_StandInPort | Exception] = [
            _StandInPort([b'1223\r\n80'], fail_after=True),
            serial.SerialException('could not open port: no such device'),
            _StandInPort([b'5\r\n1139\r\n'], fail_after=False),
        ]

        def _open_next(_self):
            """Hand out the next stand-in port, or raise the next opening failure."""
            next_port = ports_to_open.pop(0)
            if isinstance(next_port, Exception):
                raise next_port
            return next_port

        monkeypatch.setattr(ssl_mod.SerialSyncLogger, '_open_port', _open_next)
        logger = SerialSyncLogger(port='/dev/ttyACM0', baud_rate=9600, capture_directory=tmp_path, capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.01)
        capture_file = logger.start_capture()
        _wait_until(lambda: logger.bytes_written == len(b'1223\r\n805\r\n1139\r\n'))
        logger.stop_capture()
        assert capture_file.read_bytes() == b'1223\r\n805\r\n1139\r\n'
        assert len(logger.events) == 2
        assert 'lost /dev/ttyACM0' in logger.events[0]
        assert 'reopened /dev/ttyACM0' in logger.events[1]

    def test_unopenable_port_fails_before_capture(self, tmp_path):
        """A wrong arduino_sync_port is reported by its setting name, and no capture file is left behind."""
        logger = SerialSyncLogger(port=str(tmp_path / 'no_such_port'), baud_rate=9600, capture_directory=tmp_path / 'Data', capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.02)
        with pytest.raises(RuntimeError, match='arduino_sync_port'):
            logger.start_capture()
        assert not (tmp_path / 'Data').exists()

    @pytest.mark.skipif(sys.platform == 'win32', reason='D:\\CoolTerm is an absolute path on Windows')
    def test_relative_capture_directory_refused(self, tmp_path, monkeypatch):
        """The shipped Windows directory (D:\\CoolTerm) is a relative path elsewhere; it must not create a stray folder."""
        monkeypatch.chdir(tmp_path)
        logger = SerialSyncLogger(port='loop://', baud_rate=9600, capture_directory='D:\\CoolTerm/Data', capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.02)
        with pytest.raises(ValueError, match='coolterm_basedirectory'):
            logger.start_capture()
        assert list(tmp_path.iterdir()) == []

    def test_start_twice_refused_and_stop_twice_harmless(self, tmp_path):
        """A running capture cannot be restarted, and stopping again (or before starting) does nothing."""
        logger = SerialSyncLogger(port='loop://', baud_rate=9600, capture_directory=tmp_path, capture_prefix='CoolTerm Capture (usv-playpen)', read_timeout_s=0.02)
        assert logger.stop_capture() == 0
        logger.start_capture()
        with pytest.raises(RuntimeError, match='already been started'):
            logger.start_capture()
        logger.stop_capture()
        assert logger.stop_capture() == 0
