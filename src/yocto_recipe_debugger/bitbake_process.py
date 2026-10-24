"""Start backend.py inside the BitBake environment and talk to it."""

import itertools
import json
import os
import selectors
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

BACKEND_SOURCE = Path(__file__).with_name("backend.py")

# Runs inside the wrapper.  $1 is the init script, $2 the build directory,
# $3 the backend source.  oe-init-build-env prints a banner on stdout,
# which would break the protocol, so it goes to stderr.
BOOTSTRAP = '. "$1" "$2" >&2 && exec python3 -c "$3"'


class BackendError(Exception):
    """The backend reported an error, or could not be started."""

    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None,
                 code: Optional[str] = None):
        super().__init__(message)
        self.file = file
        self.line = line
        # "busy" when another BitBake uses the build directory.
        self.code = code


@dataclass
class BitbakeSettings:
    build_dir: str
    # oe-init-build-env; found next to the build directory when empty.
    init_script: str = ""
    # Command prefix that enters the BitBake environment, for example
    # "docker run --rm -i -v /work:/work image".  Empty runs on the host.
    command_wrapper: str = ""
    extra_env: dict = field(default_factory=dict)
    # The first analysis after a configuration change parses every recipe.
    timeout: float = 1800

    def resolved_init_script(self) -> str:
        if self.init_script:
            return self.init_script
        found = find_init_script(self.build_dir)
        if not found:
            raise BackendError(
                "Cannot find oe-init-build-env near %s. Set initScript." % self.build_dir)
        return found

    def command(self) -> list:
        wrapper = shlex.split(self.command_wrapper) if self.command_wrapper else []
        return wrapper + [
            "bash", "-c", BOOTSTRAP, "yrd",
            self.resolved_init_script(), self.build_dir, BACKEND_SOURCE.read_text(),
        ]


def find_init_script(build_dir: str) -> Optional[str]:
    """Look for oe-init-build-env in the parent of the build directory.

    Covers the common layouts "<top>/poky/oe-init-build-env" and
    "<top>/layers/poky/oe-init-build-env" next to "<top>/build".
    """
    top = Path(build_dir).resolve().parent
    for candidate in (top / "oe-init-build-env",
                      *sorted(top.glob("*/oe-init-build-env")),
                      *sorted(top.glob("*/*/oe-init-build-env"))):
        if candidate.is_file():
            return str(candidate)
    return None


class BitbakeProcess:
    """A long-lived backend process.  Requests are serialized."""

    def __init__(self, settings: BitbakeSettings,
                 on_stderr: Optional[Callable[[str], None]] = None):
        self.settings = settings
        self.on_stderr = on_stderr or (lambda line: None)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def _start(self) -> subprocess.Popen:
        env = dict(os.environ)
        env.update(self.settings.extra_env)
        proc = subprocess.Popen(
            self.settings.command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()
        return proc

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            self.on_stderr(line.rstrip("\n"))

    def request(self, method: str, params: Optional[dict] = None) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._start()
            proc = self._proc
            request_id = next(self._ids)
            try:
                proc.stdin.write(json.dumps({"id": request_id, "method": method,
                                             "params": params or {}}) + "\n")
                proc.stdin.flush()
            except BrokenPipeError:
                pass
            if not self._wait_readable(proc, self.settings.timeout):
                proc.kill()
                proc.wait()
                self._proc = None
                raise BackendError("BitBake did not answer in %d seconds." % self.settings.timeout)
            line = proc.stdout.readline()
            if not line:
                code = proc.wait()
                self._proc = None
                raise BackendError(
                    "The BitBake backend exited (code %s). See the output log." % code)
            response = json.loads(line)
            if response.get("id") != request_id:
                raise BackendError("Protocol error: unexpected response id")
            if "error" in response:
                err = response["error"]
                raise BackendError(err.get("message", "unknown error"),
                                   err.get("file"), err.get("line"), err.get("code"))
            return response["result"]

    @staticmethod
    def _wait_readable(proc: subprocess.Popen, timeout: float) -> bool:
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)
            return bool(sel.select(timeout))

    def analyze(self, file: str) -> dict:
        return self.request("analyze", {"file": file})

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.stdin.close()
                try:
                    self._proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
