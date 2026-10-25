"""In-process guards that keep tests off the host: real agent CLIs, network, and project discovery.

Each guard patches through *patch*. ``tests/conftest.py`` applies them per test as autouse
fixtures; the isolated runner's subprocess applies all of them through ``install_test_guards``.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import httpx2
import pytest
import requests

from agnette.agm.project import layout as project_layout
from agnette.core import http as core_http
from tests._external_agent_clis import EXTERNAL_AGENT_CLIS
from tests._http_helpers import FakeHttp


def install_test_guards(patch: pytest.MonkeyPatch, temp_root: Path) -> None:
    """Apply every in-process guard; *temp_root* is pytest's base temporary directory."""
    refuse_real_external_agent_clis(patch, temp_root)
    refuse_real_http_requests(patch)
    refuse_real_httpx2_requests(patch)
    fence_project_discovery_at_temp_root(patch, temp_root)
    detach_installed_agm_prefix(patch)


def refuse_real_external_agent_clis(patch: pytest.MonkeyPatch, temp_root: Path) -> None:
    """Refuse every in-process spawn of an external agent CLI that no test faked.

    A test's fake CLI lives under *temp_root*; resolving one of ``EXTERNAL_AGENT_CLIS``
    anywhere else would run a real agent, so ``Popen`` raises and a forked ``execvp`` child
    exits 127 instead. Subprocess ``agm`` runs in the e2e suite are guarded by its PATH shims.
    """
    fakes_root = temp_root.resolve()
    popen_init = subprocess.Popen.__init__
    popen_signature = inspect.signature(popen_init)
    execvp = os.execvp

    def real_agent_cli(program: str, search_path: str | None) -> str | None:
        if Path(program).name not in EXTERNAL_AGENT_CLIS:
            return None
        resolved = shutil.which(program, path=search_path)
        if resolved is None or Path(resolved).resolve().is_relative_to(fakes_root):
            return None
        return resolved

    def guarded_popen_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        bound = popen_signature.bind(self, *args, **kwargs).arguments
        command = bound["args"]
        if bound.get("executable") is not None:
            program = os.fsdecode(bound["executable"])
        elif isinstance(command, (str, bytes, os.PathLike)):
            words = os.fsdecode(command).split()
            program = words[0] if words else ""
        else:
            program = os.fsdecode(command[0]) if command else ""
        env = bound.get("env")
        real = real_agent_cli(program, (os.environ if env is None else env).get("PATH"))
        if real is not None:
            raise AssertionError(f"test tried to run the real external agent CLI {real}")
        popen_init(self, *args, **kwargs)

    def guarded_execvp(file: str, args: Sequence[str]) -> NoReturn:
        if real_agent_cli(file, os.environ.get("PATH")) is not None:
            os.write(2, f"test tried to run the real external agent CLI {file}\n".encode())
            os._exit(127)
        execvp(file, args)

    patch.setattr(subprocess.Popen, "__init__", guarded_popen_init)
    patch.setattr(os, "execvp", guarded_execvp)


def refuse_real_http_requests(patch: pytest.MonkeyPatch) -> None:
    """Default the HTTP transport to one that refuses any request.

    Patch ``agnette.core.http.open_session`` to build a session through the real function --
    preserving its proxy/cookie/``.netrc`` configuration -- with an empty-script ``FakeHttp``
    mounted, so an unscripted request fails immediately instead of dialing out.

    A test that scripts HTTP (``tests._http_helpers.install``/``fake_session``, or a corpus
    scenario's own patch) patches the same name again afterward, which supersedes this default.
    The real function is captured once, before patching, so ``fake_session``'s own call to
    ``open_session`` does not recurse into the replacement.
    """
    real_open_session = core_http.open_session

    def refused_session() -> requests.Session:
        session = real_open_session()
        adapter = FakeHttp([])
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    patch.setattr(core_http, "open_session", refused_session)


def refuse_real_httpx2_requests(patch: pytest.MonkeyPatch) -> None:
    """Fail any real ``httpx2`` network send and strip the TypeSafe SDK's environment.

    ``httpx2.HTTPTransport`` is the network transport an ``httpx2`` client (such as the
    TypeSafe SDK's) uses unless given another; a scripted ``httpx2.MockTransport``
    (``tests/_jev_helpers.py``) is unaffected. Inherited ``TYPESAFE_*`` variables would
    configure the SDK's key, URL, model, and logging from the host.
    """
    for name in list(os.environ):
        if name.startswith("TYPESAFE_"):
            patch.delenv(name, raising=False)

    def refused(self: httpx2.HTTPTransport, request: httpx2.Request) -> NoReturn:
        pytest.fail(f"unscripted real network request: {request.method} {request.url}")

    patch.setattr(httpx2.HTTPTransport, "handle_request", refused)


def fence_project_discovery_at_temp_root(patch: pytest.MonkeyPatch, temp_root: Path) -> None:
    """Stop ancestor-walking project discovery at *temp_root*.

    Discovery walks every ancestor of its starting directory. ``HOME`` (and so the AgM home)
    is already kept out of the way, but ``TMPDIR`` itself is host-controlled -- an agent
    sandbox routinely confines writable paths to somewhere under the invoking user's real
    home -- so a real AgM project sitting above the temporary root would otherwise decide what
    a test's temporary tree resolves to. Every directory above *temp_root* belongs to the
    host, never to a test.
    """
    host_dirs = frozenset(temp_root.resolve().parents)
    discover = project_layout._project_dir_from_workspace

    def fenced_discover(
        workspace_dir: Path, *, env: Mapping[str, str] | None = None
    ) -> Path | None:
        return None if workspace_dir in host_dirs else discover(workspace_dir, env=env)

    patch.setattr(project_layout, "_project_dir_from_workspace", fenced_discover)


def detach_installed_agm_prefix(patch: pytest.MonkeyPatch) -> None:
    """Hide any AgM installed at the test runner's own prefix."""
    patch.setattr("agnette.config.home.agm_installation_prefix", lambda: None)
