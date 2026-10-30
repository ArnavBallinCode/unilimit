"""A tiny fault-injecting TCP proxy for reliability tests.

Modes:
- ``forward``: normal operation.
- ``blackhole``: accept and hold connections but never forward bytes (timeouts).
``reset()`` abruptly closes every live connection (simulates idle drops and restarts).
"""

from __future__ import annotations

import contextlib
import select
import socket
import threading


class FaultProxy:
    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.mode = "forward"
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port: int = self._listener.getsockname()[1]
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(client)
            threading.Thread(target=self._pipe, args=(client,), daemon=True).start()

    def _pipe(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            while not self._closed:
                if self.mode == "blackhole":
                    readable, _, _ = select.select([client], [], [], 0.05)
                    if readable and not client.recv(65536, socket.MSG_PEEK):
                        return
                    continue
                if upstream is None:
                    upstream = socket.create_connection(("127.0.0.1", self.target_port))
                    with self._lock:
                        self._conns.append(upstream)
                readable, _, _ = select.select([client, upstream], [], [], 0.05)
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
        except (OSError, ValueError):  # ValueError: select() on a socket reset() closed
            return
        finally:
            for conn in (client, upstream):
                if conn is not None:
                    with contextlib.suppress(OSError):
                        conn.close()

    def reset(self) -> None:
        with self._lock:
            conns, self._conns = self._conns, []
        for sock in conns:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self._closed = True
        self.reset()
        self._listener.close()
