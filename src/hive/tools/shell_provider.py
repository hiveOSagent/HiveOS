"""
shell_provider.py — terminal-environment abstraction (M9-c, Hermes #11).

Decouples the Shell builtin from the local subprocess so execution can be
rerouted to a container or remote host without changing the tool contract.
Default: LocalShellProvider (wraps asyncio.create_subprocess_shell).
DockerShellProvider: runs commands inside a disposable container with
network isolation (HIVE_SHELL_PROVIDER=docker, HIVE_SHELL_DOCKER_IMAGE).
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ShellResult:
    stdout: str
    returncode: int


def _without_approver_key(env: dict[str, str] | None) -> dict[str, str]:
    """Return a child environment that never carries the approver credential."""
    source = os.environ if env is None else env
    return {key: value for key, value in source.items() if key != "HIVE_APPROVER_KEY"}


class ShellProvider(ABC):
    """Execute a shell command and return its combined stdout/stderr."""

    @abstractmethod
    async def run(self, cmd: str, *, timeout: float = 30.0,
                  env: dict[str, str] | None = None) -> ShellResult:
        ...


class LocalShellProvider(ShellProvider):
    """Run commands in the local process environment via asyncio subprocess."""

    async def run(self, cmd: str, *, timeout: float = 30.0,
                  env: dict[str, str] | None = None) -> ShellResult:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_without_approver_key(env),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ShellResult(
            stdout=stdout.decode(errors="replace"),
            returncode=proc.returncode if proc.returncode is not None else 0,
        )


class DockerShellProvider(ShellProvider):
    """Run commands inside a disposable Docker container for filesystem/network isolation.

    Wired via HIVE_SHELL_PROVIDER=docker and HIVE_SHELL_DOCKER_IMAGE (default: alpine:latest).
    Network is set to 'none' by default — override with network='bridge' when outbound
    access is needed inside the container.
    """

    def __init__(self, image: str = "alpine:latest", *, network: str = "none") -> None:
        self._image = image
        self._network = network

    async def run(self, cmd: str, *, timeout: float = 30.0,
                  env: dict[str, str] | None = None) -> ShellResult:
        safe_env = _without_approver_key(env)
        env_args = " ".join(f"-e {shlex.quote(k + '=' + v)}" for k, v in safe_env.items())
        full = (
            f"docker run --rm --network {self._network} "
            f"{env_args} {self._image} sh -c {shlex.quote(cmd)}"
        )
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
        # Avoid inheriting the approver key into the Docker CLI process.  Keep
        # the legacy call shape when the parent does not carry the key so
        # existing provider fakes and callers remain compatible.
        if "HIVE_APPROVER_KEY" in os.environ:
            kwargs["env"] = _without_approver_key(None)
        proc = await asyncio.create_subprocess_shell(full, **kwargs)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ShellResult(
            stdout=stdout.decode(errors="replace"),
            returncode=proc.returncode if proc.returncode is not None else 0,
        )
