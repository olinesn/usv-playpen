"""
@author: olinesn
Runs Avisoft RECORDER USGH under Wine on Linux and macOS, through usbio-shim.

The UltraSoundGate 1216H is not a USB audio device: on Windows, RECORDER USGH
drives it through Avisoft's kernel driver (``usgh_x64.sys``, a renamed build of
Thesycon's USBIO generic driver), which Wine cannot load. usbio-shim stands in
for that driver. It has two parts:

* ``powrprof.dll``, a 32-bit DLL placed beside ``rec_usgh.exe``. RECORDER loads
  it at start-up; it redirects RECORDER's calls to the driver to the helper and
  passes everything else through.
* ``usbio-helper``, a native program that does the USB work with libusb and
  listens on 127.0.0.1 for the DLL.

Avisoft's files are not modified. This module starts the helper and RECORDER,
and stops both at the end of a recording.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
from typing import IO


def resolve_avisoft_runner(avisoft_runner_setting: str,
                           operating_system: str) -> str:
    """
    Description
    -----------
    Decides how RECORDER USGH is started for a recording.

    ``'auto'`` keeps the Windows launch (PowerShell ``Start-Process``) on
    Windows, as before, and runs RECORDER under Wine on every other system.
    ``'native'`` and ``'wine'`` force one of the two; each is refused on the
    systems where it cannot work.

    Parameters
    ----------
    avisoft_runner_setting (str)
        The ``avisoft_runner`` setting: ``'auto'``, ``'native'`` or ``'wine'``.
    operating_system (str)
        The system name as ``platform.system()`` returns it (``'Windows'``,
        ``'Linux'``, ``'Darwin'``).

    Returns
    -------
    avisoft_runner (str)
        ``'native'`` or ``'wine'``.
    """

    if avisoft_runner_setting not in ('auto', 'native', 'wine'):
        msg = (f"avisoft_runner must be 'auto', 'native' or 'wine', "
               f"got {avisoft_runner_setting!r}.")
        raise ValueError(msg)

    if avisoft_runner_setting == 'auto':
        return 'native' if operating_system == 'Windows' else 'wine'

    if avisoft_runner_setting == 'native' and operating_system != 'Windows':
        msg = (f"avisoft_runner = 'native' works on Windows only (this is {operating_system}); "
               f"set it to 'auto' or 'wine' to run RECORDER USGH under Wine.")
        raise ValueError(msg)

    if avisoft_runner_setting == 'wine' and operating_system == 'Windows':
        msg = "avisoft_runner = 'wine' works on Linux and macOS only; on Windows set it to 'auto' or 'native'."
        raise ValueError(msg)

    return avisoft_runner_setting


def wine_windows_path(path: str | pathlib.PurePath,
                      wine_prefix: str | pathlib.PurePath) -> str:
    """
    Description
    -----------
    Gives the Windows path under which a program running in the Wine prefix
    sees a local path: ``C:\\...`` for a path inside the prefix's ``drive_c``,
    and ``Z:\\...`` for any other path (every Wine prefix maps ``Z:`` to ``/``).
    RECORDER USGH reads its paths in this form from ``avisoft_config.ini`` and
    writes them in this form into its channel logs.

    No Wine command is run, so the result does not depend on Wine being
    installed; a leading ``~`` is expanded. Symbolic links are kept as given.

    Parameters
    ----------
    path (str | pathlib.PurePath)
        Absolute local path.
    wine_prefix (str | pathlib.PurePath)
        The Wine prefix RECORDER USGH is installed in.

    Returns
    -------
    windows_path (str)
        The path as Windows programs in the prefix see it.
    """

    local_path = pathlib.PurePosixPath(pathlib.Path(str(path)).expanduser())
    if not local_path.is_absolute():
        msg = f"Paths handed to RECORDER USGH under Wine must be absolute, got {str(path)!r}."
        raise ValueError(msg)

    drive_c = pathlib.PurePosixPath(pathlib.Path(str(wine_prefix)).expanduser()) / 'drive_c'
    if local_path == drive_c or drive_c in local_path.parents:
        return 'C:\\' + '\\'.join(local_path.relative_to(drive_c).parts)

    return 'Z:\\' + '\\'.join(local_path.parts[1:])


def process_command_name(pid: int) -> str | None:
    """
    Description
    -----------
    The name of the program a process runs, as ``ps`` reports it.

    Parameters
    ----------
    pid (int)
        The process.

    Returns
    -------
    name (str | None)
        The program's file name, or None if there is no such process.
    """

    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = result.stdout.strip()
    return pathlib.PurePosixPath(name).name if name else None


def wait_for_free_port(port: int,
                       max_wait_s: float) -> bool:
    """
    Description
    -----------
    Waits until a TCP port on 127.0.0.1 can be listened on again.

    Parameters
    ----------
    port (int)
        The port.
    max_wait_s (float)
        How long (s) to wait.

    Returns
    -------
    free (bool)
        True once the port is free; False if it is still taken.
    """

    deadline = time.monotonic() + max_wait_s
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                pass
            else:
                return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


class WineAvisoftRecorder:
    """
    Description
    -----------
    Starts RECORDER USGH under Wine with usbio-shim, and stops it.

    :meth:`start` checks the installation, copies the shim's ``powrprof.dll``
    beside ``rec_usgh.exe`` (only when it differs), starts ``usbio-helper``,
    waits until it listens, and starts ``wine rec_usgh.exe /CFG=... /AUT``.
    Wine loads the DLL from the program's folder because the launch sets
    ``WINEDLLOVERRIDES=powrprof=n,b`` for RECORDER only, so nothing is written
    to the prefix's registry. Any Wine process left in the prefix by an earlier
    run is ended first, so the prefix should hold RECORDER USGH only.

    :meth:`stop` asks RECORDER to close as a user would (Wine's ``taskkill``
    without ``/f`` sends its window a close message), forces it only if it is
    still open after ``close_max_wait_s``, then stops the helper.

    The helper's and Wine's output go to ``usbio-helper.log`` and
    ``rec_usgh-wine.log`` in ``log_directory``, replaced at each start. The
    helper's process ID is kept in ``usbio-helper.pid`` there while it runs,
    so that a helper left running by a run that was interrupted (the program
    crashed or was closed mid-recording) is ended at the next start.
    """

    def __init__(self, *,
                 wine_binary: str,
                 wine_prefix: str,
                 recorder_exe_directory: str,
                 recorder_program_name: str,
                 shim_directory: str,
                 helper_port: int,
                 device_order: list[str] | tuple[str, ...],
                 log_directory: str,
                 close_max_wait_s: float,
                 helper_start_max_wait_s: float,
                 helper_stop_max_wait_s: float,
                 forced_stop_max_wait_s: float,
                 cpu_priority: str = '',
                 cpu_affinity: list[int] | tuple[int, ...] = ()) -> None:
        """
        Description
        -----------
        Initializes the WineAvisoftRecorder class. Nothing is started until
        :meth:`start`.

        Parameters
        ----------
        wine_binary (str)
            The ``wine`` command: a name looked up on ``PATH`` or a path to it.
            ``wineserver`` is looked for beside it first, then on ``PATH``.
        wine_prefix (str)
            The Wine prefix RECORDER USGH is installed in.
        recorder_exe_directory (str)
            Local path of the folder holding ``rec_usgh.exe`` inside the prefix.
        recorder_program_name (str)
            RECORDER's program file name (``rec_usgh.exe``).
        shim_directory (str)
            Folder holding the built ``powrprof.dll`` and ``usbio-helper``.
        helper_port (int)
            TCP port on 127.0.0.1 the helper listens on and the DLL connects to.
        device_order (list[str] | tuple[str, ...])
            USB serial numbers of the UltraSoundGate units in the order
            RECORDER should see them (the first is the SYNC master); empty to
            order them by USB bus and port.
        log_directory (str)
            Folder the helper's and Wine's logs are written to.
        close_max_wait_s (float)
            How long (s) RECORDER may take to close after :meth:`stop` asks it
            to, before it is ended without asking
            (``timeouts.avisoft_close_max_wait_s``).
        helper_start_max_wait_s (float)
            How long (s) the helper may take to start listening
            (``timeouts.avisoft_helper_start_max_wait_s``).
        helper_stop_max_wait_s (float)
            How long (s) the helper may take to exit once told to, and how long
            a helper left by an interrupted run may take to free its port
            (``timeouts.avisoft_helper_stop_max_wait_s``).
        forced_stop_max_wait_s (float)
            How long (s) ``wineserver -k`` and RECORDER may take to end when
            Wine's processes are ended without asking
            (``timeouts.avisoft_forced_stop_max_wait_s``).
        cpu_priority (str)
            A Windows priority class (``audio.cpu_priority``), applied to both
            processes as the nice value standing in for it; empty to leave it.
        cpu_affinity (list[int] | tuple[int, ...])
            CPUs both processes are kept on (Linux only); empty to leave it.

        Returns
        -------
        None
        """

        self.wine_binary = wine_binary
        self.wine_prefix = pathlib.Path(wine_prefix).expanduser()
        self.recorder_exe_directory = pathlib.Path(recorder_exe_directory).expanduser()
        self.recorder_program_name = recorder_program_name
        self.shim_directory = pathlib.Path(shim_directory).expanduser()
        self.helper_port = int(helper_port)
        self.device_order = [str(serial) for serial in device_order]
        self.log_directory = pathlib.Path(log_directory).expanduser()
        self.close_max_wait_s = close_max_wait_s
        self.helper_start_max_wait_s = helper_start_max_wait_s
        self.helper_stop_max_wait_s = helper_stop_max_wait_s
        self.forced_stop_max_wait_s = forced_stop_max_wait_s
        self.cpu_priority = cpu_priority
        self.cpu_affinity = list(cpu_affinity)
        # the names usbio-shim's build gives its two parts: Wine loads the DLL in
        # place of Windows' own powrprof.dll, so its name cannot change
        self.shim_dll_name = 'powrprof.dll'
        self.helper_name = 'usbio-helper'
        self.helper_log_path = self.log_directory / 'usbio-helper.log'
        self.helper_pid_path = self.log_directory / 'usbio-helper.pid'
        self.recorder_log_path = self.log_directory / 'rec_usgh-wine.log'
        self._wine_path: str | None = None
        self._wineserver_path: str | None = None
        self._helper_process: subprocess.Popen[bytes] | None = None
        self._recorder_process: subprocess.Popen[bytes] | None = None
        self._log_handles: list[IO[bytes]] = []

    def resolve_wine(self) -> tuple[str, str | None]:
        """
        Description
        -----------
        Finds ``wine`` and ``wineserver``.

        Parameters
        ----------

        Returns
        -------
        (wine_path, wineserver_path) (tuple[str, str | None])
            Full path of ``wine``, and of ``wineserver`` (None when not found;
            a forced stop then ends RECORDER's process group instead).
        """

        wine_path = shutil.which(str(pathlib.Path(self.wine_binary).expanduser()))
        if wine_path is None:
            msg = (f"Wine not found: avisoft_wine_binary is {self.wine_binary!r}. Install Wine "
                   f"(see Requirements) or give the full path to its 'wine' command.")
            raise FileNotFoundError(msg)

        wineserver_beside = pathlib.Path(wine_path).with_name('wineserver')
        if wineserver_beside.is_file() and os.access(wineserver_beside, os.X_OK):
            wineserver_path: str | None = str(wineserver_beside)
        else:
            wineserver_path = shutil.which('wineserver')

        return wine_path, wineserver_path

    def check_installation(self) -> None:
        """
        Description
        -----------
        Checks that Wine, the prefix, RECORDER USGH and the built shim are all
        where the settings say, before anything is started.

        Parameters
        ----------

        Returns
        -------
        None

        Raises
        ------
        FileNotFoundError
            Naming the first missing piece and the setting that points to it.
        """

        self._wine_path, self._wineserver_path = self.resolve_wine()

        checks = (
            (self.wine_prefix / 'system.reg', 'avisoft_wine_prefix',
             f"no Wine prefix at '{self.wine_prefix}'"),
            (self.recorder_exe_directory / self.recorder_program_name, 'avisoft_recorder_exe_directory',
             f"'{self.recorder_program_name}' is not in '{self.recorder_exe_directory}'"),
            (self.shim_directory / self.shim_dll_name, 'avisoft_usbio_shim_directory',
             f"no '{self.shim_dll_name}' in '{self.shim_directory}' (build usbio-shim first)"),
            (self.shim_directory / self.helper_name, 'avisoft_usbio_shim_directory',
             f"no '{self.helper_name}' in '{self.shim_directory}' (build usbio-shim first)"),
        )
        for path, setting, problem in checks:
            if not path.is_file():
                msg = f"Cannot start RECORDER USGH under Wine: {problem}; check {setting}."
                raise FileNotFoundError(msg)

        if not os.access(self.shim_directory / self.helper_name, os.X_OK):
            msg = f"Cannot start RECORDER USGH under Wine: '{self.shim_directory / self.helper_name}' is not executable."
            raise PermissionError(msg)

    def install_shim(self) -> bool:
        """
        Description
        -----------
        Copies the shim's ``powrprof.dll`` beside ``rec_usgh.exe`` unless an
        identical copy is already there.

        Parameters
        ----------

        Returns
        -------
        copied (bool)
            True if the DLL was copied.
        """

        source = self.shim_directory / self.shim_dll_name
        destination = self.recorder_exe_directory / self.shim_dll_name
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            return False
        shutil.copyfile(source, destination)
        return True

    def wine_environment(self) -> dict[str, str]:
        """
        Description
        -----------
        The environment Wine runs RECORDER (and ``taskkill``) with: this
        process's environment plus the prefix, the DLL override that makes Wine
        load the shim from RECORDER's folder, the helper's port, and
        ``WINEDEBUG=-all,+msgbox`` (Wine's messages off, except the text of
        each message box RECORDER shows) unless WINEDEBUG is already set.

        Parameters
        ----------

        Returns
        -------
        environment (dict[str, str])
            Environment for the Wine processes.
        """

        environment = dict(os.environ)
        environment['WINEPREFIX'] = str(self.wine_prefix)
        overrides = 'powrprof=n,b;winemenubuilder.exe=d'
        user_overrides = environment['WINEDLLOVERRIDES'] if 'WINEDLLOVERRIDES' in environment else ''
        if user_overrides:
            overrides = f"{overrides};{user_overrides}"
        environment['WINEDLLOVERRIDES'] = overrides
        environment['USBIO_SHIM_PORT'] = str(self.helper_port)
        # unless the environment sets one: Wine's own messages off, except the
        # text of every message box, so RECORDER's error boxes reach the log
        if 'WINEDEBUG' not in environment:
            environment['WINEDEBUG'] = '-all,+msgbox'
        return environment

    def helper_command(self) -> list[str]:
        """
        Description
        -----------
        The command line that starts ``usbio-helper``.

        Parameters
        ----------

        Returns
        -------
        command (list[str])
            Program and arguments.
        """

        command = [str(self.shim_directory / self.helper_name), '-p', str(self.helper_port)]
        if self.device_order:
            command += ['-o', ','.join(self.device_order)]
        return command

    def recorder_command(self) -> list[str]:
        """
        Description
        -----------
        The command line that starts RECORDER USGH under Wine with the lab's
        configuration, recording at once (``/AUT``), as on Windows.

        Parameters
        ----------

        Returns
        -------
        command (list[str])
            Program and arguments.
        """

        return [self._wine_path or self.wine_binary, self.recorder_program_name, '/CFG=avisoft_config.ini', '/AUT']

    def is_running(self) -> bool:
        """
        Description
        -----------
        Whether RECORDER's Wine process started by :meth:`start` is still running.

        Parameters
        ----------

        Returns
        -------
        running (bool)
            True while it runs.
        """

        return self._recorder_process is not None and self._recorder_process.poll() is None

    def log_tail(self, lines: int = 15) -> str:
        """
        Description
        -----------
        The last lines of the helper's log, to show why a start failed.

        Parameters
        ----------
        lines (int)
            How many lines to return.

        Returns
        -------
        tail (str)
            Those lines, or an empty string if there is no log.
        """

        try:
            text = self.helper_log_path.read_text(errors='replace')
        except OSError:
            return ''
        return '\n'.join(text.splitlines()[-lines:])

    def message_boxes(self) -> list[str]:
        """
        Description
        -----------
        The text of each message box RECORDER showed since :meth:`start`, read
        from Wine's log (e.g. ``UltraSoundGate hardware not found!``). Empty
        when WINEDEBUG was set to something without ``+msgbox``.

        Parameters
        ----------

        Returns
        -------
        texts (list[str])
            One entry per message box, first line only, in order.
        """

        try:
            lines = self.recorder_log_path.read_text(errors='replace').splitlines()
        except OSError:
            return []
        # the line Wine's +msgbox channel writes for each message box
        message_box_line = re.compile(r'MSGBOX_OnInit L"(.*)"\s*$')
        texts = []
        for line in lines:
            match = message_box_line.search(line)
            if match:
                texts.append(match.group(1).split('\\n')[0])
        return texts

    def _open_log(self, path: pathlib.Path) -> IO[bytes]:
        """
        Description
        -----------
        Opens one log file for a child process's output, replacing an older
        one, and keeps the handle so that :meth:`stop` closes it.

        Parameters
        ----------
        path (pathlib.Path)
            The log file.

        Returns
        -------
        handle (IO[bytes])
            The open file, for the child process's output.
        """

        handle = path.open('wb')
        self._log_handles.append(handle)
        return handle

    def _apply_scheduling(self, pid: int, label: str) -> list[str]:
        """
        Description
        -----------
        Applies ``cpu_affinity`` (Linux) and the nice value standing in for
        ``cpu_priority`` to one process. A failure is reported, not raised: the
        recording does not depend on it.

        Parameters
        ----------
        pid (int)
            The process.
        label (str)
            Its name in the notes.

        Returns
        -------
        notes (list[str])
            One line per setting that could not be applied.
        """

        notes = []
        if self.cpu_affinity:
            if hasattr(os, 'sched_setaffinity'):
                try:
                    os.sched_setaffinity(pid, set(self.cpu_affinity))
                except OSError as error:
                    notes.append(f"CPU affinity {self.cpu_affinity} not applied to {label}: {error}.")
            else:
                notes.append(f"CPU affinity is not supported on {platform.system()}; {label} runs on any CPU.")

        if self.cpu_priority:
            # Windows priority classes (the values ``audio.cpu_priority`` takes for
            # PowerShell's ``PriorityClass``) and the nice values that stand in for them
            priority_class_nice = {
                'idle': 19,
                'belownormal': 10,
                'normal': 0,
                'abovenormal': -5,
                'high': -10,
                'realtime': -20,
            }
            priority_class = self.cpu_priority.replace(' ', '').lower()
            nice = priority_class_nice[priority_class] if priority_class in priority_class_nice else None
            if nice is None:
                notes.append(f"Unknown cpu_priority {self.cpu_priority!r}; {label} keeps its priority.")
            else:
                try:
                    os.setpriority(os.PRIO_PROCESS, pid, nice)
                except OSError as error:
                    notes.append(f"Priority {self.cpu_priority} (nice {nice}) not applied to {label}: {error} "
                                 f"(raising priority needs root or CAP_SYS_NICE).")
        return notes

    def _end_leftover_wine_processes(self) -> None:
        """
        Description
        -----------
        Ends every Wine process in the prefix with ``wineserver -k`` (a RECORDER
        left open by an earlier, interrupted run). Does nothing when
        ``wineserver`` was not found.

        Parameters
        ----------

        Returns
        -------
        None
        """

        if self._wineserver_path is None:
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run([self._wineserver_path, '-k'], env=self.wine_environment(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=self.forced_stop_max_wait_s, check=False)

    def _end_leftover_helper(self) -> list[str]:
        """
        Description
        -----------
        Ends the usbio-helper whose process ID an interrupted run left in
        ``usbio-helper.pid``, if that process is still a usbio-helper, and waits
        until its port is free.

        Parameters
        ----------

        Returns
        -------
        notes (list[str])
            One line if a helper was ended.
        """

        try:
            pid = int(self.helper_pid_path.read_text().strip())
        except (OSError, ValueError):
            return []

        notes = []
        if process_command_name(pid) == self.helper_name:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
                notes.append(f"Ended {self.helper_name} (process {pid}) left running by an earlier recording.")
            wait_for_free_port(self.helper_port, self.helper_stop_max_wait_s)
        self.helper_pid_path.unlink(missing_ok=True)
        return notes

    def _start_helper(self) -> subprocess.Popen[bytes]:
        """
        Description
        -----------
        Starts ``usbio-helper`` and waits until its log says it listens.

        Parameters
        ----------

        Returns
        -------
        helper (subprocess.Popen[bytes])
            The running helper.

        Raises
        ------
        RuntimeError
            If the helper exits (e.g. the port is taken by a helper left
            running by an earlier run) or does not listen in time; the
            message holds the end of its log.
        """

        helper = subprocess.Popen(self.helper_command(),
                                  stdin=subprocess.DEVNULL,
                                  stdout=self._open_log(self.helper_log_path),
                                  stderr=subprocess.STDOUT,
                                  start_new_session=True)
        self._helper_process = helper
        deadline = time.monotonic() + self.helper_start_max_wait_s
        while True:
            try:
                # the helper's ready line: "usbio-helper listening on 127.0.0.1:<port>, ..."
                ready = 'listening on' in self.helper_log_path.read_text(errors='replace')
            except OSError:
                ready = False
            if ready:
                self.helper_pid_path.write_text(f"{helper.pid}\n")
                return helper
            if helper.poll() is not None:
                problem = f"usbio-helper exited with code {helper.returncode}"
                break
            if time.monotonic() > deadline:
                problem = f"usbio-helper did not start listening within {self.helper_start_max_wait_s:.0f} s"
                break
            time.sleep(0.05)

        tail = self.log_tail()
        self.stop()
        msg = (f"Cannot start RECORDER USGH under Wine: {problem}. "
               f"If the port {self.helper_port} is in use, a helper from an earlier run may still be "
               f"running ('pkill -x {self.helper_name}' ends it). Helper log:\n{tail}")
        raise RuntimeError(msg)

    def start(self) -> list[str]:
        """
        Description
        -----------
        Checks the installation, then starts the helper and RECORDER USGH.

        Parameters
        ----------

        Returns
        -------
        notes (list[str])
            What the caller should report: whether the DLL was updated, and any
            CPU priority or affinity that could not be applied.
        """

        self.check_installation()
        self.log_directory.mkdir(parents=True, exist_ok=True)

        notes = []
        if self.install_shim():
            notes.append(f"Installed usbio-shim's {self.shim_dll_name} beside {self.recorder_program_name}.")

        self._end_leftover_wine_processes()
        notes += self._end_leftover_helper()
        helper = self._start_helper()
        notes += self._apply_scheduling(helper.pid, self.helper_name)

        self._recorder_process = subprocess.Popen(self.recorder_command(),
                                                  cwd=self.recorder_exe_directory,
                                                  env=self.wine_environment(),
                                                  stdin=subprocess.DEVNULL,
                                                  stdout=self._open_log(self.recorder_log_path),
                                                  stderr=subprocess.STDOUT,
                                                  start_new_session=True)
        notes += self._apply_scheduling(self._recorder_process.pid, self.recorder_program_name)
        return notes

    def _force_stop_recorder(self, process: subprocess.Popen[bytes]) -> None:
        """
        Description
        -----------
        Ends RECORDER without asking: every Wine process in the prefix through
        ``wineserver -k``, or, without ``wineserver`` or if RECORDER is still
        running ``forced_stop_max_wait_s`` later, its whole process group.

        Parameters
        ----------
        process (subprocess.Popen[bytes])
            RECORDER's Wine process, started by :meth:`start`.

        Returns
        -------
        None
        """

        if self._wineserver_path is not None:
            self._end_leftover_wine_processes()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=self.forced_stop_max_wait_s)
                return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()

    def stop(self) -> list[str]:
        """
        Description
        -----------
        Closes RECORDER USGH, then stops the helper. RECORDER is ended without
        asking if it is still open ``close_max_wait_s`` after being asked to
        close. Safe to call more than once, and after a failed start.

        Parameters
        ----------

        Returns
        -------
        events (list[str])
            What happened that the caller should report (RECORDER exiting on
            its own or having to be forced, the helper not stopping).
        """

        events = []
        process = self._recorder_process
        if process is not None:
            if process.poll() is not None:
                events.append(f"RECORDER USGH had already exited (code {process.returncode}).")
            else:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run([self._wine_path or self.wine_binary, 'taskkill', '/im', self.recorder_program_name],
                                   env=self.wine_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=self.close_max_wait_s + 30, check=False)
                try:
                    process.wait(timeout=self.close_max_wait_s)
                except subprocess.TimeoutExpired:
                    events.append(f"RECORDER USGH did not close within {self.close_max_wait_s:g} s of being asked; it was ended.")
                    self._force_stop_recorder(process)
            self._recorder_process = None

        helper = self._helper_process
        if helper is not None:
            if helper.poll() is None:
                helper.terminate()
                try:
                    helper.wait(timeout=self.helper_stop_max_wait_s)
                except subprocess.TimeoutExpired:
                    events.append(f"{self.helper_name} did not stop within {self.helper_stop_max_wait_s:.0f} s; it was killed.")
                    helper.kill()
                    helper.wait()
            self._helper_process = None
            self.helper_pid_path.unlink(missing_ok=True)

        for handle in self._log_handles:
            handle.close()
        self._log_handles = []
        return events
