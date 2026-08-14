"""Client for the privileged echotray-helperd daemon.

The EchoTray GUI runs as an unprivileged user and has NO access to
/dev/uinput. All privileged input (Ctrl+V injection) is handled by root-owned
echotray-helperd, which we talk to over a Unix socket at
/run/echotray.sock.

The daemon is paste-only: the GUI sends a `paste` request and the daemon
injects Ctrl+V. There is no hotkey and no config.
"""

import json
import socket

SOCKET_PATH = "/run/echotray.sock"


class HelperError(RuntimeError):
    """Raised when the helper daemon is unreachable or misbehaving."""


class HelperClient:
    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path

    def _connect(self):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.socket_path)
            return s
        except (OSError, FileNotFoundError) as e:
            raise HelperError(f"cannot connect to {self.socket_path}: {e}")

    def _send(self, obj):
        s = self._connect()
        try:
            s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        finally:
            s.close()

    def send_paste(self):
        """Ask the daemon to inject Ctrl+V."""
        self._send({"cmd": "paste"})

    def send_paste_probe(self):
        """Open and close a connection to the daemon without sending anything.

        Used as a liveness probe: succeeds if the socket is connectable, raises
        HelperError if the daemon isn't running.
        """
        s = self._connect()
        s.close()
