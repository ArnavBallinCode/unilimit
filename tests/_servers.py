"""Real Redis server processes for tests: standalone instances and a 6-node cluster.

Uses ``$UNILIMIT_REDIS_SERVER`` (a redis-server or valkey-server binary) or
``redis-server`` on PATH. Without a binary, Redis tests are skipped, unless
``UNILIMIT_REQUIRE_REDIS=1`` (set in CI), in which case they fail.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import redis


def server_binary() -> str:
    path = os.environ.get("UNILIMIT_REDIS_SERVER") or shutil.which("redis-server")
    if not path:
        message = "redis-server not found (set UNILIMIT_REDIS_SERVER)"
        if os.environ.get("UNILIMIT_REQUIRE_REDIS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return path


def free_port() -> int:
    """A free port whose cluster bus port (+10000) is also free."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port: int = s.getsockname()[1]
        if port + 10_000 > 65_535:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port + 10_000))
            except OSError:
                continue
        return port


class RedisProcess:
    def __init__(self, binary: str, *, cluster: bool = False, extra: tuple[str, ...] = ()) -> None:
        self.binary = binary
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp(prefix=f"unilimit-redis-{self.port}-"))
        self.cluster = cluster
        self.extra = extra
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def client(self, **kwargs: object) -> redis.Redis:
        return redis.Redis(port=self.port, **kwargs)  # type: ignore[arg-type]

    def start(self) -> RedisProcess:
        args = [
            self.binary,
            "--port", str(self.port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--dir", str(self.dir),
            "--protected-mode", "no",
        ]  # fmt: skip
        if self.cluster:
            args += [
                "--cluster-enabled", "yes",
                "--cluster-config-file", "nodes.conf",
                "--cluster-node-timeout", "1000",
                # Start replication immediately so replicas are failover-eligible quickly.
                "--repl-diskless-sync-delay", "0",
            ]  # fmt: skip
        args += list(self.extra)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while True:
            try:
                if self.client(socket_timeout=0.5).ping():
                    return self
            except (redis.exceptions.AuthenticationError, redis.exceptions.ResponseError):
                return self  # answering with an auth error means the server is up
            except redis.exceptions.ConnectionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    def stop(self) -> None:
        """Kill the process (SIGKILL): an abrupt crash, not a graceful shutdown."""
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(10)
            self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def destroy(self) -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def version(self) -> str:
        info = self.client().info("server")
        name = "valkey" if "valkey_version" in info else "redis"
        return f"{name} {info.get('valkey_version') or info['redis_version']}"


class RedisCluster:
    """Three primaries and three replicas on localhost."""

    def __init__(self, binary: str, primaries: int = 3, replicas: int = 1) -> None:
        self.nodes = [RedisProcess(binary, cluster=True) for _ in range(primaries * (1 + replicas))]
        self.primaries = primaries
        self.replicas = replicas

    @property
    def url(self) -> str:
        return self.nodes[0].url

    def start(self) -> RedisCluster:
        for node in self.nodes:
            node.start()
        cli = Path(self.nodes[0].binary).with_name(
            Path(self.nodes[0].binary).name.replace("server", "cli")
        )
        if not cli.exists():
            found = shutil.which(cli.name)
            if found is None:
                pytest.skip(f"{cli.name} not found; needed to create a test cluster")
            cli = Path(found)
        subprocess.run(
            [
                str(cli),
                "--cluster",
                "create",
                *(f"127.0.0.1:{n.port}" for n in self.nodes),
                "--cluster-replicas",
                str(self.replicas),
                "--cluster-yes",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.wait_ok()
        return self

    def wait_ok(self, timeout: float = 30) -> None:
        """Wait until every live node reports the cluster OK and every replica has
        completed its initial sync. Unsynced replicas are never promoted on failover."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                live = [n for n in self.nodes if n.proc is not None]
                infos = [n.client(socket_timeout=0.5).cluster("info") for n in live]
                replication = [n.client(socket_timeout=0.5).info("replication") for n in live]
                if all(i["cluster_state"] == "ok" for i in infos) and all(
                    r["master_link_status"] == "up" for r in replication if r["role"] == "slave"
                ):
                    return
            except redis.exceptions.RedisError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("cluster did not become healthy")
            time.sleep(0.1)

    def primary_clients(self) -> list[redis.Redis]:
        out = []
        for node in self.nodes:
            if node.proc is None:
                continue
            client = node.client()
            if client.info("replication")["role"] == "master":
                out.append(client)
        return out

    def node_for_slot(self, slot: int) -> RedisProcess:
        """The primary currently serving ``slot`` (from CLUSTER SLOTS)."""
        live = next(n for n in self.nodes if n.proc is not None)
        for entry in live.client().execute_command("CLUSTER", "SLOTS"):
            start, end, primary = int(entry[0]), int(entry[1]), entry[2]
            if start <= slot <= end:
                port = int(primary[1])
                return next(n for n in self.nodes if n.port == port)
        raise LookupError(slot)

    def flushall(self) -> None:
        for client in self.primary_clients():
            client.flushall()

    def destroy(self) -> None:
        for node in self.nodes:
            node.destroy()
