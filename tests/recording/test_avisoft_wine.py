"""
@author: olinesn
Test usv_playpen.recording.avisoft_wine: RECORDER USGH under Wine through usbio-shim.

No Wine, RECORDER USGH or UltraSoundGate is needed. The launch and stop are
run end to end against stand-ins written into tmp_path: a ``wine`` that plays
RECORDER (connects to the helper's port, prints a message-box trace line as
Wine's ``+msgbox`` channel does, and runs until Wine's ``taskkill`` asks it to
close or ``wineserver -k`` ends it), a ``wineserver``, and a ``usbio-helper``
that listens on its port and prints the helper's ready line.

Coverage: resolve_avisoft_runner; wine_windows_path; process_command_name;
wait_for_free_port; WineAvisoftRecorder's installation checks, DLL install,
environment, command lines, message-box parsing, start and stop (graceful,
forced through wineserver, forced through the process group), a helper that
fails to start, and a helper left running by an earlier run.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
from typing import Any

import pytest

import usv_playpen.recording.avisoft_wine as aw_mod
from usv_playpen.recording.avisoft_wine import (
    WineAvisoftRecorder,
    process_command_name,
    resolve_avisoft_runner,
    wait_for_free_port,
    wine_windows_path,
)

posix_only = pytest.mark.skipif(sys.platform == 'win32', reason="RECORDER runs under Wine on Linux and macOS only")

# Plays `wine`: `wine taskkill ...` asks RECORDER to close; anything else plays RECORDER USGH.
FAKE_WINE = r'''
import json, os, pathlib, socket, sys, time
state = pathlib.Path(os.environ['FAKE_WINE_STATE'])
if sys.argv[1] == 'taskkill':
    (state / 'taskkill.json').write_text(json.dumps(sys.argv[1:]))
    (state / 'close').touch()
    sys.exit(0)
for marker in ('close', 'kill'):
    (state / marker).unlink(missing_ok=True)
with socket.create_connection(('127.0.0.1', int(os.environ['USBIO_SHIM_PORT'])), timeout=5):
    pass
record = {'argv': sys.argv[1:], 'cwd': os.getcwd(), 'pid': os.getpid(),
          'env': {key: os.environ.get(key) for key in ('WINEPREFIX', 'WINEDLLOVERRIDES', 'USBIO_SHIM_PORT', 'WINEDEBUG')}}
(state / 'recorder.json').write_text(json.dumps(record))
print('0024:trace:msgbox:MSGBOX_OnInit L"Test box\\n\\nsecond line"', flush=True)
ignore_close = os.environ.get('FAKE_WINE_IGNORE_CLOSE') == '1'
while not ((state / 'kill').exists() or ((state / 'close').exists() and not ignore_close)):
    time.sleep(0.02)
'''

# Plays `wineserver`: `-k` ends every Wine process in the prefix.
FAKE_WINESERVER = r'''
import os, pathlib, sys
state = pathlib.Path(os.environ['FAKE_WINE_STATE'])
with (state / 'wineserver.log').open('a') as log:
    log.write(' '.join(sys.argv[1:]) + '\n')
if sys.argv[1:] == ['-k']:
    (state / 'kill').touch()
'''

# Plays usbio-helper: listens on -p PORT and prints the ready line (or fails, on request).
FAKE_HELPER = r'''
import json, os, pathlib, socket, sys
args = sys.argv[1:]
port = int(args[args.index('-p') + 1])
state = pathlib.Path(os.environ['FAKE_WINE_STATE'])
(state / 'helper.json').write_text(json.dumps({'argv': args, 'pid': os.getpid()}))
if os.environ.get('FAKE_HELPER_FAIL') == '1':
    print('usbio-helper: bind/listen: Address already in use', file=sys.stderr, flush=True)
    sys.exit(1)
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('127.0.0.1', port))
server.listen(8)
print(f'[     0.000] usbio-helper listening on 127.0.0.1:{port}, serving vendor 0547 (libusb ok)', file=sys.stderr, flush=True)
connections = []
while True:
    connection, _ = server.accept()
    connections.append(connection)
'''


def _write_program(path: pathlib.Path, source: str) -> pathlib.Path:
    """
    Description
    -----------
    Writes a Python stand-in program, run by this test's interpreter, and makes
    it executable.

    Parameters
    ----------
    path (pathlib.Path)
        Where the program is written; its folder is created if missing.
    source (str)
        The program's Python source, without the ``#!`` line.

    Returns
    -------
    path (pathlib.Path)
        The written program.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(0o755)
    return path


def _free_port() -> int:
    """
    Description
    -----------
    Finds a TCP port on 127.0.0.1 that nothing listens on.

    Parameters
    ----------

    Returns
    -------
    port (int)
        The port.
    """

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def wine_rig(tmp_path, monkeypatch):
    """A Wine prefix with RECORDER USGH, a built usbio-shim and stand-in programs, all under tmp_path.

    Yields a dict of paths plus ``make``, which builds a WineAvisoftRecorder for
    this rig; every recorder made is stopped at teardown so no stand-in outlives
    the test."""
    bin_dir = tmp_path / 'bin'
    wine = _write_program(bin_dir / 'wine', FAKE_WINE)
    _write_program(bin_dir / 'wineserver', FAKE_WINESERVER)
    prefix = tmp_path / 'prefix'
    exe_dir = prefix / 'drive_c' / 'Program Files (x86)' / 'Avisoft Bioacoustics' / 'RECORDER USGH'
    exe_dir.mkdir(parents=True)
    (prefix / 'system.reg').write_text('WINE REGISTRY Version 2\n')
    (exe_dir / 'rec_usgh.exe').write_bytes(b'MZ')
    shim_dir = tmp_path / 'usbio-shim' / 'out'
    shim_dir.mkdir(parents=True)
    (shim_dir / 'powrprof.dll').write_bytes(b'MZ shim dll')
    _write_program(shim_dir / 'usbio-helper', FAKE_HELPER)
    base = tmp_path / 'avisoft'
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('FAKE_WINE_STATE', str(state))
    monkeypatch.delenv('WINEDEBUG', raising=False)
    monkeypatch.delenv('WINEDLLOVERRIDES', raising=False)
    port = _free_port()
    made = []

    def make(**overrides):
        """Builds a WineAvisoftRecorder for the rig, with any argument overridden."""
        arguments = {
            'wine_binary': str(wine),
            'wine_prefix': str(prefix),
            'recorder_exe_directory': str(exe_dir),
            'recorder_program_name': 'rec_usgh.exe',
            'shim_directory': str(shim_dir),
            'helper_port': port,
            'device_order': [],
            'log_directory': str(base),
            'close_max_wait_s': 10,
            'helper_start_max_wait_s': 5,
            'helper_stop_max_wait_s': 5,
            'forced_stop_max_wait_s': 10,
        }
        arguments.update(overrides)
        recorder = WineAvisoftRecorder(**arguments)
        made.append(recorder)
        return recorder

    yield {'wine': wine, 'prefix': prefix, 'exe_dir': exe_dir, 'shim_dir': shim_dir,
           'base': base, 'state': state, 'port': port, 'make': make}

    for recorder in made:
        recorder.close_max_wait_s = 1
        recorder.stop()


def _read_json(path: pathlib.Path, timeout_s: float = 10.0) -> dict[str, Any]:
    """
    Description
    -----------
    Waits for a stand-in program to write its JSON record, then reads it. Fails
    the test if the record never appears.

    Parameters
    ----------
    path (pathlib.Path)
        The record the stand-in writes.
    timeout_s (float)
        How long (s) to wait for it.

    Returns
    -------
    record (dict[str, Any])
        The parsed record.
    """

    deadline = time.monotonic() + timeout_s
    while not path.is_file():
        if time.monotonic() > deadline:
            pytest.fail(f"{path.name} was never written")
        time.sleep(0.02)
    while True:
        try:
            record: dict[str, Any] = json.loads(path.read_text())
        except json.JSONDecodeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
        else:
            return record


def _port_is_listening(port: int) -> bool:
    """
    Description
    -----------
    Whether something accepts connections on a TCP port of 127.0.0.1.

    Parameters
    ----------
    port (int)
        The port.

    Returns
    -------
    listening (bool)
        True if a connection was accepted.
    """

    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


# resolve_avisoft_runner


@pytest.mark.parametrize(('setting', 'system', 'expected'), [
    ('auto', 'Windows', 'native'),
    ('auto', 'Linux', 'wine'),
    ('auto', 'Darwin', 'wine'),
    ('native', 'Windows', 'native'),
    ('wine', 'Linux', 'wine'),
    ('wine', 'Darwin', 'wine'),
])
def test_resolve_avisoft_runner_keeps_windows_native_and_uses_wine_elsewhere(setting, system, expected):
    """'auto' keeps the Windows launch on Windows and picks Wine elsewhere; forced values pass through."""
    assert resolve_avisoft_runner(setting, system) == expected


@pytest.mark.parametrize(('setting', 'system', 'match'), [
    ('native', 'Linux', 'Windows only'),
    ('native', 'Darwin', 'Windows only'),
    ('wine', 'Windows', 'Linux and macOS only'),
    ('WINE', 'Linux', "must be 'auto', 'native' or 'wine'"),
    ('', 'Linux', "must be 'auto', 'native' or 'wine'"),
])
def test_resolve_avisoft_runner_refuses_a_runner_the_system_cannot_use(setting, system, match):
    """A runner that cannot work on this system, or an unknown value, is refused with a reason."""
    with pytest.raises(ValueError, match=match):
        resolve_avisoft_runner(setting, system)


# wine_windows_path


def test_wine_windows_path_outside_prefix_maps_to_z():
    """A path outside the prefix's drive_c is seen through Z:, which every prefix maps to /."""
    assert wine_windows_path('/data/avisoft', '/home/rig/.wine-avisoft') == 'Z:\\data\\avisoft'
    assert wine_windows_path('/data/avisoft/', '/home/rig/.wine-avisoft') == 'Z:\\data\\avisoft'
    assert wine_windows_path(pathlib.PurePosixPath('/data/my recordings/ch1'), '/p') == 'Z:\\data\\my recordings\\ch1'
    assert wine_windows_path('/', '/p') == 'Z:\\'


def test_wine_windows_path_inside_drive_c_maps_to_c():
    """A path inside drive_c is given as C:, as Windows programs in the prefix see it."""
    prefix = '/home/rig/.wine-avisoft'
    config = f"{prefix}/drive_c/users/rig/Documents/Avisoft Bioacoustics/Configurations/RECORDER_USGH/avisoft_config.ini"
    assert wine_windows_path(config, prefix) == (
        'C:\\users\\rig\\Documents\\Avisoft Bioacoustics\\Configurations\\RECORDER_USGH\\avisoft_config.ini')
    assert wine_windows_path(f"{prefix}/drive_c", prefix) == 'C:\\'
    # a sibling folder that only starts with the same letters is not inside drive_c
    assert wine_windows_path(f"{prefix}/drive_cx/a", prefix) == 'Z:\\home\\rig\\.wine-avisoft\\drive_cx\\a'


def test_wine_windows_path_expands_home(monkeypatch):
    """A leading ~ is expanded in both the path and the prefix."""
    monkeypatch.setenv('HOME', '/home/rig')
    assert wine_windows_path('~/avisoft', '~/.wine-avisoft') == 'Z:\\home\\rig\\avisoft'
    assert wine_windows_path('~/.wine-avisoft/drive_c/x', '~/.wine-avisoft') == 'C:\\x'


def test_wine_windows_path_refuses_relative_path():
    """RECORDER cannot be given a relative path (its working directory is its program folder)."""
    with pytest.raises(ValueError, match='must be absolute'):
        wine_windows_path('avisoft', '/p')


# process_command_name / wait_for_free_port


@posix_only
def test_process_command_name_names_a_live_process_and_none_for_a_gone_one():
    """ps names this test's own interpreter; a process that has exited has no name."""
    assert 'python' in (process_command_name(os.getpid()) or '').lower()
    finished = subprocess.Popen([sys.executable, '-c', 'pass'])
    finished.wait()
    assert process_command_name(finished.pid) is None


def test_wait_for_free_port_reports_taken_and_free():
    """A port someone listens on is not free; once they stop, it is."""
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert wait_for_free_port(port, max_wait_s=0.2) is False
    assert wait_for_free_port(port, max_wait_s=2) is True


# WineAvisoftRecorder: checks, DLL install, environment, commands, message boxes


@posix_only
def test_check_installation_passes_on_a_complete_rig(wine_rig):
    """Wine, the prefix, RECORDER and the built shim are all found; wineserver is taken from beside wine."""
    recorder = wine_rig['make']()
    recorder.check_installation()
    wine_path, wineserver_path = recorder.resolve_wine()
    assert wine_path == str(wine_rig['wine'])
    assert wineserver_path == str(wine_rig['wine'].with_name('wineserver'))


@posix_only
@pytest.mark.parametrize(('breakage', 'setting'), [
    ('wine', 'avisoft_wine_binary'),
    ('prefix', 'avisoft_wine_prefix'),
    ('exe', 'avisoft_recorder_exe_directory'),
    ('dll', 'avisoft_usbio_shim_directory'),
    ('helper', 'avisoft_usbio_shim_directory'),
])
def test_check_installation_names_the_missing_piece(wine_rig, breakage, setting):
    """Each missing piece is refused before anything starts, naming the setting that points to it."""
    overrides = {}
    if breakage == 'wine':
        overrides['wine_binary'] = str(wine_rig['wine'].with_name('no-such-wine'))
    elif breakage == 'prefix':
        (wine_rig['prefix'] / 'system.reg').unlink()
    elif breakage == 'exe':
        (wine_rig['exe_dir'] / 'rec_usgh.exe').unlink()
    elif breakage == 'dll':
        (wine_rig['shim_dir'] / 'powrprof.dll').unlink()
    else:
        (wine_rig['shim_dir'] / 'usbio-helper').unlink()
    recorder = wine_rig['make'](**overrides)
    with pytest.raises(FileNotFoundError, match=setting):
        recorder.check_installation()


@posix_only
def test_check_installation_refuses_a_helper_that_is_not_executable(wine_rig):
    """A helper copied without its executable bit is reported as such."""
    (wine_rig['shim_dir'] / 'usbio-helper').chmod(0o644)
    with pytest.raises(PermissionError, match='not executable'):
        wine_rig['make']().check_installation()


@posix_only
def test_install_shim_copies_only_when_the_dll_differs(wine_rig):
    """The DLL is copied beside rec_usgh.exe once, and again only after it changes."""
    recorder = wine_rig['make']()
    installed = wine_rig['exe_dir'] / 'powrprof.dll'
    assert recorder.install_shim() is True
    assert installed.read_bytes() == b'MZ shim dll'
    assert recorder.install_shim() is False
    (wine_rig['shim_dir'] / 'powrprof.dll').write_bytes(b'MZ shim dll, rebuilt')
    assert recorder.install_shim() is True
    assert installed.read_bytes() == b'MZ shim dll, rebuilt'


def test_wine_environment_sets_prefix_shim_override_and_port(monkeypatch, tmp_path):
    """The prefix, the shim's DLL override (ahead of any the user set) and the port are set; a user's WINEDEBUG is kept."""
    monkeypatch.setenv('WINEDLLOVERRIDES', 'mscoree=d')
    monkeypatch.delenv('WINEDEBUG', raising=False)
    recorder = WineAvisoftRecorder(wine_binary='wine', wine_prefix=str(tmp_path / 'prefix'),
                                   recorder_exe_directory=str(tmp_path), recorder_program_name='rec_usgh.exe',
                                   shim_directory=str(tmp_path), helper_port=47999, device_order=[],
                                   log_directory=str(tmp_path),
                                   close_max_wait_s=10, helper_start_max_wait_s=5,
                                   helper_stop_max_wait_s=5, forced_stop_max_wait_s=10)
    environment = recorder.wine_environment()
    assert environment['WINEPREFIX'] == str(tmp_path / 'prefix')
    assert environment['WINEDLLOVERRIDES'] == 'powrprof=n,b;winemenubuilder.exe=d;mscoree=d'
    assert environment['USBIO_SHIM_PORT'] == '47999'
    assert environment['WINEDEBUG'] == '-all,+msgbox'
    monkeypatch.setenv('WINEDEBUG', '+relay')
    assert recorder.wine_environment()['WINEDEBUG'] == '+relay'


def test_commands_pass_port_device_order_and_lab_config(tmp_path):
    """The helper gets its port and the device order; RECORDER gets the lab's config and /AUT, as on Windows."""
    recorder = WineAvisoftRecorder(wine_binary='wine', wine_prefix=str(tmp_path), recorder_exe_directory=str(tmp_path),
                                   recorder_program_name='rec_usgh.exe', shim_directory=str(tmp_path / 'shim'),
                                   helper_port=47321, device_order=['A1234', 5678], log_directory=str(tmp_path),
                                   close_max_wait_s=10, helper_start_max_wait_s=5,
                                   helper_stop_max_wait_s=5, forced_stop_max_wait_s=10)
    assert recorder.helper_command() == [str(tmp_path / 'shim' / 'usbio-helper'), '-p', '47321', '-o', 'A1234,5678']
    assert recorder.recorder_command() == ['wine', 'rec_usgh.exe', '/CFG=avisoft_config.ini', '/AUT']
    recorder.device_order = []
    assert recorder.helper_command() == [str(tmp_path / 'shim' / 'usbio-helper'), '-p', '47321']


def test_message_boxes_reads_wine_msgbox_traces(tmp_path):
    """Each message box RECORDER showed is read from Wine's trace lines, first line only."""
    recorder = WineAvisoftRecorder(wine_binary='wine', wine_prefix=str(tmp_path), recorder_exe_directory=str(tmp_path),
                                   recorder_program_name='rec_usgh.exe', shim_directory=str(tmp_path),
                                   helper_port=47321, device_order=[], log_directory=str(tmp_path),
                                   close_max_wait_s=10, helper_start_max_wait_s=5,
                                   helper_stop_max_wait_s=5, forced_stop_max_wait_s=10)
    assert recorder.message_boxes() == []
    recorder.recorder_log_path.write_text(
        'wine: some unrelated line\n'
        '00d4:trace:msgbox:MSGBOX_OnInit L"No UltraSoundGate hardware found!"\n'
        '00e4:trace:msgbox:MSGBOX_OnInit L"UltraSoundGate hardware or UltraSoundGate device driver not found!'
        '\\n\\nIf you have updated..."\r\n')
    assert recorder.message_boxes() == ['No UltraSoundGate hardware found!',
                                        'UltraSoundGate hardware or UltraSoundGate device driver not found!']


# WineAvisoftRecorder: start and stop, end to end against the stand-ins


@posix_only
def test_start_and_stop_end_to_end(wine_rig):
    """start installs the DLL, starts the helper, then RECORDER in its folder with the shim's environment;
    stop asks RECORDER to close, stops the helper and leaves no PID file."""
    recorder = wine_rig['make'](device_order=['MASTER1', 'SLAVE2'])
    notes = recorder.start()
    assert notes == [f"Installed usbio-shim's {'powrprof.dll'} beside rec_usgh.exe."]
    assert (wine_rig['exe_dir'] / 'powrprof.dll').read_bytes() == b'MZ shim dll'

    started = _read_json(wine_rig['state'] / 'recorder.json')
    assert started['argv'] == ['rec_usgh.exe', '/CFG=avisoft_config.ini', '/AUT']
    assert pathlib.Path(started['cwd']).resolve() == wine_rig['exe_dir'].resolve()
    assert started['env'] == {'WINEPREFIX': str(wine_rig['prefix']),
                              'WINEDLLOVERRIDES': 'powrprof=n,b;winemenubuilder.exe=d',
                              'USBIO_SHIM_PORT': str(wine_rig['port']),
                              'WINEDEBUG': '-all,+msgbox'}
    helper = _read_json(wine_rig['state'] / 'helper.json')
    assert helper['argv'] == ['-p', str(wine_rig['port']), '-o', 'MASTER1,SLAVE2']
    assert (wine_rig['base'] / 'usbio-helper.pid').read_text().strip() == str(helper['pid'])
    assert 'listening on' in (wine_rig['base'] / 'usbio-helper.log').read_text()
    assert recorder.is_running()
    # an earlier run's Wine processes in the prefix were ended before the start
    assert (wine_rig['state'] / 'wineserver.log').read_text().splitlines() == ['-k']

    assert recorder.stop() == []
    assert not recorder.is_running()
    assert json.loads((wine_rig['state'] / 'taskkill.json').read_text()) == ['taskkill', '/im', 'rec_usgh.exe']
    assert (wine_rig['state'] / 'wineserver.log').read_text().splitlines() == ['-k']
    assert not (wine_rig['base'] / 'usbio-helper.pid').exists()
    assert wait_for_free_port(wine_rig['port'], max_wait_s=5)
    assert recorder.message_boxes() == ['Test box']

    # a second start leaves the identical DLL alone
    assert recorder.start() == []
    recorder.stop()


@posix_only
def test_stop_forces_a_recorder_that_does_not_close(wine_rig, monkeypatch):
    """A RECORDER still open after close_max_wait_s is ended through wineserver -k, and that is reported."""
    monkeypatch.setenv('FAKE_WINE_IGNORE_CLOSE', '1')
    recorder = wine_rig['make'](close_max_wait_s=0.3)
    recorder.start()
    _read_json(wine_rig['state'] / 'recorder.json')
    events = recorder.stop()
    assert events == ["RECORDER USGH did not close within 0.3 s of being asked; it was ended."]
    assert not recorder.is_running()
    assert (wine_rig['state'] / 'wineserver.log').read_text().splitlines() == ['-k', '-k']


@posix_only
def test_stop_ends_the_process_group_without_wineserver(wine_rig, monkeypatch):
    """Without a wineserver to call, a RECORDER that does not close is ended with its process group."""
    monkeypatch.setenv('FAKE_WINE_IGNORE_CLOSE', '1')
    monkeypatch.setattr(WineAvisoftRecorder, 'resolve_wine', lambda _self: (str(wine_rig['wine']), None))
    recorder = wine_rig['make'](close_max_wait_s=0.3)
    recorder.start()
    _read_json(wine_rig['state'] / 'recorder.json')
    events = recorder.stop()
    assert events == ["RECORDER USGH did not close within 0.3 s of being asked; it was ended."]
    assert not recorder.is_running()
    assert not (wine_rig['state'] / 'wineserver.log').exists()


@posix_only
def test_stop_reports_a_recorder_that_already_exited(wine_rig):
    """A RECORDER that exited by itself (e.g. after an error box) is reported, not asked to close."""
    recorder = wine_rig['make']()
    recorder.start()
    _read_json(wine_rig['state'] / 'recorder.json')
    (wine_rig['state'] / 'kill').touch()
    deadline = time.monotonic() + 10
    while recorder.is_running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert recorder.stop() == ["RECORDER USGH had already exited (code 0)."]
    assert not (wine_rig['state'] / 'taskkill.json').exists()


@posix_only
def test_start_fails_cleanly_when_the_helper_exits(wine_rig, monkeypatch):
    """A helper that exits (e.g. its port is taken) stops the start with its log, and nothing is left running."""
    monkeypatch.setenv('FAKE_HELPER_FAIL', '1')
    recorder = wine_rig['make']()
    with pytest.raises(RuntimeError, match='usbio-helper exited with code 1') as raised:
        recorder.start()
    assert 'Address already in use' in str(raised.value)
    assert not recorder.is_running()
    assert not (wine_rig['state'] / 'recorder.json').exists()
    assert not (wine_rig['base'] / 'usbio-helper.pid').exists()


@posix_only
def test_start_fails_when_the_helper_never_listens(wine_rig):
    """A helper that never prints its ready line is given up on after the start budget."""
    _write_program(wine_rig['shim_dir'] / 'usbio-helper', 'import time\ntime.sleep(60)\n')
    recorder = wine_rig['make'](helper_start_max_wait_s=0.3)
    with pytest.raises(RuntimeError, match='did not start listening'):
        recorder.start()
    assert not recorder.is_running()


@posix_only
def test_start_ends_a_helper_left_running_by_an_earlier_run(wine_rig, monkeypatch):
    """The helper named in usbio-helper.pid is ended (it holds the port) before a new one starts."""
    leftover = subprocess.Popen([str(wine_rig['shim_dir'] / 'usbio-helper'), '-p', str(wine_rig['port'])],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not _port_is_listening(wine_rig['port']):
            assert time.monotonic() < deadline, "the leftover helper never listened"
            time.sleep(0.02)
        wine_rig['base'].mkdir()
        (wine_rig['base'] / 'usbio-helper.pid').write_text(f"{leftover.pid}\n")
        # ps names a Python stand-in after its interpreter on macOS, so name it as the real helper would be
        real_process_command_name = aw_mod.process_command_name
        monkeypatch.setattr(aw_mod, 'process_command_name',
                            lambda pid: 'usbio-helper' if pid == leftover.pid else real_process_command_name(pid))

        recorder = wine_rig['make']()
        notes = recorder.start()
        assert f"Ended usbio-helper (process {leftover.pid}) left running by an earlier recording." in notes
        assert leftover.wait(timeout=10) != 0
        new_pid = int((wine_rig['base'] / 'usbio-helper.pid').read_text())
        assert new_pid != leftover.pid
        recorder.stop()
    finally:
        if leftover.poll() is None:
            leftover.kill()
        leftover.wait()


@posix_only
def test_start_leaves_an_unrelated_process_named_in_a_stale_pid_file(wine_rig):
    """A PID file naming a process that is not a usbio-helper (the PID was reused) is discarded, not acted on."""
    wine_rig['base'].mkdir()
    (wine_rig['base'] / 'usbio-helper.pid').write_text(f"{os.getpid()}\n")
    recorder = wine_rig['make']()
    notes = recorder.start()
    assert not any('left running' in note for note in notes)
    recorder.stop()


@posix_only
def test_scheduling_reports_what_it_cannot_apply(wine_rig, monkeypatch):
    """CPU priority and affinity are applied where possible and reported where not, never raised."""
    calls = []
    monkeypatch.setattr(aw_mod.os, 'setpriority', lambda _which, pid, nice: calls.append(('nice', pid, nice)), raising=False)

    def refuse_affinity(_pid, _cpus):
        """Stands in for sched_setaffinity on a machine that refuses it."""
        raise OSError(22, 'Invalid argument')

    monkeypatch.setattr(aw_mod.os, 'sched_setaffinity', refuse_affinity, raising=False)
    recorder = wine_rig['make'](cpu_priority='High', cpu_affinity=[2, 3])
    assert recorder._apply_scheduling(1234, 'rec_usgh.exe') == [
        "CPU affinity [2, 3] not applied to rec_usgh.exe: [Errno 22] Invalid argument."]
    assert calls == [('nice', 1234, -10)]

    recorder = wine_rig['make'](cpu_priority='Turbo')
    assert recorder._apply_scheduling(1234, 'x') == ["Unknown cpu_priority 'Turbo'; x keeps its priority."]

    def refuse_priority(_which, _pid, _nice):
        """Stands in for setpriority without the right to raise priority."""
        raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(aw_mod.os, 'setpriority', refuse_priority, raising=False)
    recorder = wine_rig['make'](cpu_priority='RealTime')
    assert recorder._apply_scheduling(1, 'x') == [
        ("Priority RealTime (nice -20) not applied to x: [Errno 1] Operation not permitted "
         "(raising priority needs root or CAP_SYS_NICE).")]
