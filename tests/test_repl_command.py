"""Tests for the ``agm repl`` CLI command and its config wiring.

Covers:
- the CLI surface maps each flag onto ``ReplArgs`` (parser-contract style;
  ``repl.run`` is mocked so no real terminal is needed);
- ``--no-trace`` / ``--trace-file`` are mutually exclusive (usage error, exit 1);
- there is no ``--input`` option: the REPL has no entry function, so there
  is no pre-seed CLI option;
- ``repl.run`` resolves ``[exec]`` config, builds a session, and hands off to
  a REPL front end (mocked) with the echo flag and a history path derived from
  AgM home.
- front-end selection: ``--plain``, and the ``plain_mode_engaged`` predicate,
  decide between the plain and prompt_toolkit front ends.

Most of these tests run under pytest, where stdin/stdout are typically not
real terminals, so the *default* front end actually invoked is plain; the
``_args()`` helper defaults ``plain=True`` to make that explicit and
deterministic rather than relying on the test process's incidental tty state.
The two tests that specifically verify prompt_toolkit-console wiring force
that front end explicitly (``plain=False`` plus a patched engagement
predicate) so they do not depend on whether the test runner has a tty either.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import pytest
from click.testing import CliRunner, Result
from typer.main import get_command

import agnette.agm.cli as cli
import agnette.agm.commands.repl as repl_command
from agnette.agent.spec import AgentPi, AgentSpec, PermissionMode
from agnette.agl.ir.static_keys import HostParamKey
from agnette.agl.repl import ReplSession
from agnette.agl.runtime.sessions import SessionSnapshot
from agnette.agl.runtime.types import HostParamInfo
from agnette.agm.cli_support.args import ReplArgs
from agnette.config.general import GeneralConfig
from agnette.packages.layout import MODULE_TREE_DIRNAME
from agnette.packages.stdlib import StdlibSelection
from agnette.sandbox.request import SandboxLimits
from tests._engine_flags import engine_flag_tokens


class RecordedArgs(Protocol):
    def __getattr__(self, name: str) -> object: ...


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, argv: list[str]) -> Result:
    return runner.invoke(get_command(cli.app), argv, prog_name="agm", catch_exceptions=False)


@pytest.fixture()
def recorded_runs(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Patch ``repl.run`` to record its ``ReplArgs`` instead of starting a REPL."""
    calls: list[object] = []

    def fake_run(args: object, **_: object) -> None:
        calls.append(args)

    monkeypatch.setattr(repl_command, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Parser-contract: flags → ReplArgs
# ---------------------------------------------------------------------------


class TestReplArgsParsing:
    def test_bare_repl_defaults(self, runner: CliRunner, recorded_runs: list[object]) -> None:
        result = invoke(runner, ["repl"])
        assert result.exit_code == 0
        args = recorded_runs[0]
        assert getattr(args, "tokens") == []
        assert getattr(args, "quiet") is False
        assert getattr(args, "plain") is False

    def test_dry_run_option_removed(self, runner: CliRunner) -> None:
        result = invoke(runner, ["repl", "--dry-run"])
        assert result.exit_code != 0

    def test_help_omits_dry_run_option(self, runner: CliRunner) -> None:
        result = invoke(runner, ["repl", "--help"])
        assert result.exit_code == 0
        assert "--dry-run" not in result.output

    def test_quiet_flag(self, runner: CliRunner, recorded_runs: list[object]) -> None:
        assert invoke(runner, ["repl", "--quiet"]).exit_code == 0
        assert getattr(recorded_runs[0], "quiet") is True

    @pytest.mark.parametrize(("flag", "expected"), [("--echo", True), ("--no-echo", False)])
    def test_agent_output_echo_flags(
        self, runner: CliRunner, recorded_runs: list[object], flag: str, expected: bool
    ) -> None:
        assert invoke(runner, ["repl", flag]).exit_code == 0
        assert getattr(recorded_runs[0], "echo") is expected

    def test_plain_flag(self, runner: CliRunner, recorded_runs: list[object]) -> None:
        assert invoke(runner, ["repl", "--plain"]).exit_code == 0
        assert getattr(recorded_runs[0], "plain") is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["--strict-json"],
            ["--no-strict-json"],
            ["--agent", 'AgentCommand("echo agent")'],
            ["--trace-file", "/tmp/r.log"],
            ["--no-trace"],
            ["--timeout", "5s"],
            ["--no-timeout"],
        ],
    )
    def test_engine_setting_flags_reach_the_tokens(
        self, runner: CliRunner, recorded_runs: list[object], argv: list[str]
    ) -> None:
        assert invoke(runner, ["repl", *argv]).exit_code == 0
        assert getattr(recorded_runs[0], "tokens") == argv


class TestReplEngineFlagErrors:
    """Malformed engine-setting flags are reported before any session starts."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--no-trace-file"],
            ["--input", "a=1"],
            ["stray"],
            ["--no-trace", "--trace-file", "/tmp/x.log"],
            ["--timeout", "5s", "--no-timeout"],
            ["--trace-file", "x.jsonl", "--trace"],
        ],
    )
    def test_unknown_or_conflicting_flags_exit_1(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        argv: list[str],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        result = invoke(runner, ["repl", "--plain", *argv])
        assert result.exit_code != 0
        assert not (tmp_path / "x.jsonl").exists()


def test_agent_cancellation_is_reported_without_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agnette.agl.runtime.request import AgentRequest, AgentResponse
    from agnette.agl.runtime.sessions import AgentDispatcherSessionHost
    from agnette.config.context import ConfigContext

    monkeypatch.setattr(
        repl_command,
        "current_config_context",
        lambda: ConfigContext(home=tmp_path / "home", proj_dir=None, cwd=tmp_path),
    )

    def interrupted_agent(_request: AgentRequest) -> AgentResponse:
        raise KeyboardInterrupt

    monkeypatch.setattr(repl_command, "value_driven_agent_factory", lambda **_: interrupted_agent)
    monkeypatch.setattr(
        repl_command,
        "create_agl_session_host",
        lambda **_: AgentDispatcherSessionHost(interrupted_agent),
    )

    result = runner.invoke(
        get_command(cli.app),
        ["repl", "--plain"],
        prog_name="agm",
        input='ask("hi")\n',
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Agent call cancelled" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# repl.run config resolution + handoff to run_console
# ---------------------------------------------------------------------------


class _ReplConsoleCall(Protocol):
    session: ReplSession
    echo: bool
    history_path: Path


@pytest.fixture()
def fake_plain_console(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Patch the plain front end so ``repl.run`` never blocks on real stdin.

    ``_args()`` defaults ``plain=True``, so this is the front end virtually
    every ``repl.run`` config-wiring test below actually exercises.
    """
    calls: list[dict[str, object]] = []

    def fake_run_plain_console(
        session: ReplSession,
        *,
        echo: bool = True,
        echo_unit: bool = False,
        theme: str = "auto",
        on_setting_save: object = None,
        stdin: object = None,
        stdout: object = None,
    ) -> None:
        calls.append(
            {
                "session": session,
                "echo": echo,
                "echo_unit": echo_unit,
                "theme": theme,
                "on_setting_save": on_setting_save,
            }
        )

    # The command imports ``run_plain_console`` lazily from the plain_console module.
    import agnette.agl.repl.plain_console as plain_console_mod

    monkeypatch.setattr(plain_console_mod, "run_plain_console", fake_run_plain_console)
    return calls


@pytest.fixture()
def fake_console(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Patch the prompt_toolkit front end and force ``repl.run`` to select it.

    Used only by the tests that specifically verify console-only wiring (e.g.
    ``history_path``, which the plain front end has no use for): patches both
    ``run_console`` and the engagement predicate (to ``False``), so selecting
    this front end does not depend on the test process's own tty state —
    callers must still pass ``plain=False`` explicitly since ``--plain`` short
    -circuits the predicate.
    """
    calls: list[dict[str, object]] = []

    def fake_run_console(
        session: ReplSession,
        *,
        echo: bool = True,
        echo_unit: bool = False,
        history_path: Path | None = None,
        theme: str = "auto",
        on_setting_save: object = None,
        input: object = None,
        output: object = None,
    ) -> None:
        calls.append(
            {
                "session": session,
                "echo": echo,
                "echo_unit": echo_unit,
                "history_path": history_path,
                "theme": theme,
                "on_setting_save": on_setting_save,
            }
        )

    # The command imports ``run_console`` lazily from the console module.
    import agnette.agl.repl.console as console_mod

    monkeypatch.setattr(console_mod, "run_console", fake_run_console)
    monkeypatch.setattr(repl_command, "plain_mode_engaged", lambda **_kwargs: False)
    return calls


def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the config context at an isolated HOME with no project config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGM_PROJECT_DIR", raising=False)
    return tmp_path


def _args(
    *,
    strict_json: bool | None = None,
    quiet: bool = False,
    no_trace: bool = False,
    trace: bool = False,
    trace_file: str | None = None,
    timeout: str | None = None,
    no_timeout: bool = False,
    agent: str | None = None,
    sandbox: str | None = None,
    no_stdlib: bool = False,
    parse_error_retries: int | None = None,
    plain: bool = True,
) -> ReplArgs:
    """Build ``ReplArgs`` with sensible defaults, overriding named fields.

    ``plain`` defaults to ``True``: most of these tests only care about
    session/config wiring, not which front end runs it, and forcing the plain
    front end keeps that deterministic regardless of the test process's own
    tty state (see the module docstring).
    """
    return ReplArgs(
        quiet=quiet,
        tokens=engine_flag_tokens(
            strict_json=strict_json,
            no_trace=no_trace,
            trace=trace,
            trace_file=trace_file,
            timeout=timeout,
            no_timeout=no_timeout,
            agent=agent,
            sandbox=sandbox,
            parse_error_retries=parse_error_retries,
        ),
        no_stdlib=no_stdlib,
        plain=plain,
    )


class TestReplRun:
    def test_repl_exit_closes_the_injected_session_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        closed: list[None] = []

        class SessionHost:
            def close_all(self) -> None:
                closed.append(None)

        host = SessionHost()

        def create_host(**kwargs: object) -> SessionHost:
            del kwargs
            return host

        monkeypatch.setattr(repl_command, "create_agl_session_host", create_host)
        repl_command.run(_args(plain=False))

        assert closed == [None]

    def test_repl_evaluates_sessions_through_the_injected_host_and_cleans_up_on_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)

        class SessionHost:
            def __init__(self) -> None:
                self._sessions: dict[str, tuple[AgentSpec, str]] = {}
                self._sandboxing: dict[str, tuple[PermissionMode, SandboxLimits | None]] = {}
                self._envs: dict[str, dict[str, str]] = {}
                self._default_handle: str | None = None
                self.opened: list[str] = []
                self.prompts: list[tuple[str, str]] = []
                self.close_calls = 0
                self.closed_handles: set[str] = set()

            def open(
                self,
                agent: AgentSpec,
                transport: str,
                *,
                name: str = "",
                permission_mode: PermissionMode = PermissionMode.NONE,
                sandbox: SandboxLimits | None = None,
                env: dict[str, str] | None = None,
            ) -> str:
                del name
                assert isinstance(agent, AgentPi)
                handle = f"session-{len(self._sessions) + 1}"
                self._sessions[handle] = (agent, transport)
                self._sandboxing[handle] = (permission_mode, sandbox)
                self._envs[handle] = env or {}
                self.opened.append(agent.provider)
                return handle

            def default(
                self,
                agent: AgentSpec,
                transport: str,
                *,
                name: str = "",
                permission_mode: PermissionMode = PermissionMode.NONE,
                sandbox: SandboxLimits | None = None,
                env: dict[str, str] | None = None,
            ) -> str:
                if self._default_handle is None:
                    self._default_handle = self.open(
                        agent,
                        transport,
                        name=name,
                        permission_mode=permission_mode,
                        sandbox=sandbox,
                        env=env,
                    )
                return self._default_handle

            def ask(self, handle: str, prompt: str) -> str:
                self.prompts.append((handle, prompt))
                return "answer"

            def snapshot(self, handle: str) -> SessionSnapshot:
                agent, transport = self._sessions[handle]
                permission_mode, sandbox = self._sandboxing[handle]
                return SessionSnapshot(
                    agent,
                    transport,
                    permission_mode,
                    sandbox,
                    env=self._envs[handle],
                    continues_conversation=True,
                )

            def close_all(self) -> None:
                self.close_calls += 1
                self.closed_handles.update(self._sessions)

        hosts: list[SessionHost] = []

        def create_host(**kwargs: object) -> SessionHost:
            del kwargs
            host = SessionHost()
            hosts.append(host)
            return host

        def run_console(session: ReplSession, **_kwargs: object) -> None:
            result = session.eval_entry(
                "import std/config\n"
                'std/config::agent := AgentPi("default", "model", "high")\n'
                'let opened = Session::open(AgentPi("opened", "model", "high"))\n'
                "let default = Session::default()\n"
                'opened.ask("open prompt")\n'
                'default.ask("default prompt")'
            )
            assert result.ok

        import agnette.agl.repl.console as console_mod

        monkeypatch.setattr(console_mod, "run_console", run_console)
        monkeypatch.setattr(repl_command, "create_agl_session_host", create_host)

        repl_command.run(_args(plain=False))

        assert len(hosts) == 1
        host = hosts[0]
        assert host.opened == ["opened", "default"]
        assert host.prompts == [("session-1", "open prompt"), ("session-2", "default prompt")]
        assert host.close_calls == 1
        assert host.closed_handles == {"session-1", "session-2"}

    def test_repl_exit_preserves_keyboard_interrupt_when_cleanup_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)

        class SessionHost:
            def close_all(self) -> None:
                raise RuntimeError("cleanup failed")

        monkeypatch.setattr(
            repl_command, "create_agl_session_host", lambda **_kwargs: SessionHost()
        )

        def interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        import agnette.agl.repl.console as console_mod

        monkeypatch.setattr(console_mod, "run_console", interrupt)
        with pytest.raises(KeyboardInterrupt) as interrupted:
            repl_command.run(_args(plain=False))
        assert any("cleanup failed" in note for note in interrupted.value.__notes__)

    def test_builds_session_and_runs_console(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(plain=False))

        assert len(fake_console) == 1
        call = fake_console[0]
        assert isinstance(call["session"], ReplSession)
        assert call["echo"] is True
        assert call["history_path"] == home / ".agm" / "repl_history"
        assert (home / ".agm").is_dir()

    def test_builds_session_and_runs_plain_console(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args())

        assert len(fake_plain_console) == 1
        call = fake_plain_console[0]
        assert isinstance(call["session"], ReplSession)
        assert call["echo"] is True
        assert (home / ".agm").is_dir()

    def test_on_setting_save_persists_the_theme_to_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """The ``on_setting_save`` callback passed to the front end persists to config.

        Both front ends receive the same callback (built once in ``repl.run``);
        it is exercised here directly rather than by driving a live loop.
        """
        home = _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args())

        on_setting_save = fake_plain_console[0]["on_setting_save"]
        assert callable(on_setting_save)
        on_setting_save("theme", "light")

        config_text = (home / ".agm" / "config.toml").read_text(encoding="utf-8")
        assert 'theme = "light"' in config_text

    def test_on_setting_save_persists_echo_and_echo_unit_to_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args())

        on_setting_save = fake_plain_console[0]["on_setting_save"]
        assert callable(on_setting_save)
        on_setting_save("echo", False)
        on_setting_save("echo-unit", True)

        config_text = (home / ".agm" / "config.toml").read_text(encoding="utf-8")
        assert "echo = false" in config_text
        assert "echo-unit = true" in config_text

    def test_persisted_echo_settings_are_loaded_at_repl_startup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir()
        (agm_dir / "config.toml").write_text("[repl]\necho = false\necho-unit = true\n")

        repl_command.run(_args())

        call = fake_plain_console[0]
        assert call["echo"] is False
        assert call["echo_unit"] is True

    def test_quiet_overrides_saved_echo_true_for_this_session_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir()
        (agm_dir / "config.toml").write_text("[repl]\necho = true\n")

        repl_command.run(_args(quiet=True))

        assert fake_plain_console[0]["echo"] is False
        # --quiet never persists: the saved value is untouched.
        config_text = (agm_dir / "config.toml").read_text(encoding="utf-8")
        assert "echo = true" in config_text

    def test_explicit_set_persists_even_when_quiet_already_matches_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An explicit ``:set echo off`` persists even though ``--quiet`` already reads that way.

        ``--quiet`` forces the live session's ``echo`` to ``False`` without
        touching the saved ``[repl] echo = true``. Typing ``:set echo off`` in
        that session requests no live change, but it is still an explicit
        command whose target value must be saved -- drives the real plain
        front end (unfaked) over piped stdin/stdout so the actual meta-command
        dispatch and persistence wiring both run.
        """
        import io

        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir()
        (agm_dir / "config.toml").write_text("[repl]\necho = true\n")
        monkeypatch.setattr(sys, "stdin", io.StringIO(":set echo off\n"))
        monkeypatch.setattr(sys, "stdout", io.StringIO())

        repl_command.run(_args(quiet=True, plain=True))

        config_text = (agm_dir / "config.toml").read_text(encoding="utf-8")
        assert "echo = false" in config_text

    def test_invalid_development_package_exits_before_opening_the_console(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path / "home")
        (tmp_path / "package.toml").write_text("not valid TOML")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit):
            repl_command.run(_args())

        assert fake_plain_console == []

    def test_repl_excludes_the_immutable_store_from_development_discovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        calls: list[Path] = []
        original = repl_command.discover_development_packages

        def discover(anchor: Path, *, home: Path) -> tuple[object, ...]:
            calls.append(home)
            return original(anchor, home=home)

        monkeypatch.setattr(repl_command, "discover_development_packages", discover)

        repl_command.run(_args(no_stdlib=True))

        assert calls == [home]

    def test_repl_mounts_the_current_package_and_its_path_dependencies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.agl.semantics.values import IntValue

        _isolated_home(monkeypatch, tmp_path / "home")
        bravo = tmp_path / "bravo"
        (bravo / MODULE_TREE_DIRNAME).mkdir(parents=True)
        (bravo / "package.toml").write_text('[package]\nname = "bravo"\nversion = "1.0.0"\n')
        (bravo / MODULE_TREE_DIRNAME / "shared.agl").write_text("def answer() -> int = 42\n")

        alpha = tmp_path / "alpha"
        alpha.mkdir()
        (alpha / MODULE_TREE_DIRNAME).mkdir()
        (alpha / "package.toml").write_text(
            '[package]\nname = "alpha"\nversion = "1.0.0"\n\n'
            "[dependencies]\n"
            'bravo = { version = "1", path = "../bravo" }\n'
        )
        (alpha / MODULE_TREE_DIRNAME / "main.agl").write_text(
            "import bravo/shared\ndef value() -> int = bravo/shared::answer()\n"
        )
        monkeypatch.chdir(alpha)

        repl_command.run(_args(no_stdlib=True))
        session: ReplSession = fake_plain_console[0]["session"]
        result = session.eval_entry("import alpha/main\nalpha/main::value()")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(42)

    def test_cli_agent_seeds_and_repl_write_persists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.agl.runtime.render import render_value
        from agnette.agl.semantics.values import RecordValue, TextValue

        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(agent='AgentCommand("configured")'))
        session: ReplSession = fake_plain_console[0]["session"]

        assert session.eval_entry("import std/config").ok
        seeded = session.eval_entry("std/config::agent")
        assert seeded.ok
        assert isinstance(seeded.value, RecordValue)
        assert "AgentCommand(" in render_value(seeded.value, session.descriptors())
        assert seeded.value.fields["command"] == TextValue("configured")

        assert session.eval_entry('std/config::agent := AgentClaude("haiku", "low")').ok
        result = session.eval_entry("std/config::agent")
        assert result.ok
        assert isinstance(result.value, RecordValue)
        assert "AgentClaude(" in render_value(result.value, session.descriptors())
        assert result.value.fields["model"] == TextValue("haiku")

    def test_cli_sandbox_seed_is_readable_from_the_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.agl.runtime.render import render_value
        from agnette.agl.semantics.values import RecordValue

        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(sandbox='Sandbox(memory = Some("8G"))'))
        session: ReplSession = fake_plain_console[0]["session"]

        assert session.eval_entry("import std/config").ok
        seeded = session.eval_entry("std/config::sandbox")
        assert seeded.ok
        assert isinstance(seeded.value, RecordValue)
        rendered = render_value(seeded.value, session.descriptors())
        assert "Sandbox(" in rendered
        assert "8G" in rendered

    def test_repl_run_wires_a_real_sandbox_context_into_the_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """``repl.run`` -- the real production path -- threads a working
        ``get_sandbox_context`` into the session's runtime, exactly like
        ``exec_command.run`` does. A host that forgot to wire one would make a
        sandboxed ``exec`` raise ``ExecError`` instead of running it."""
        from agnette.sandbox.prepare import SandboxContext

        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args())
        session: ReplSession = fake_plain_console[0]["session"]

        get_sandbox_context = session._runtime._get_sandbox_context
        assert get_sandbox_context is not None
        assert isinstance(get_sandbox_context(), SandboxContext)

    def test_cli_agent_override_still_applies_after_reset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """``:reset`` clears the session's cached stdlib, but the override reapplies."""
        from agnette.agl.runtime.render import render_value
        from agnette.agl.semantics.values import RecordValue, TextValue

        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(agent='AgentCommand("configured")'))
        session: ReplSession = fake_plain_console[0]["session"]

        assert session.eval_entry("import std/config").ok
        session.reset()

        result = session.eval_entry("import std/config\nstd/config::agent")
        assert result.ok
        assert isinstance(result.value, RecordValue)
        assert "AgentCommand(" in render_value(result.value, session.descriptors())
        assert result.value.fields["command"] == TextValue("configured")

    def test_config_timeout_seed_still_applies_after_reset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """``:reset`` clears the cached stdlib, but the config-seeded timeout reapplies."""
        from agnette.agl.semantics.values import RecordValue, TextValue

        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir(parents=True, exist_ok=True)
        (agm_dir / "config.toml").write_text('[exec]\ntimeout = "30s"\n')

        repl_command.run(_args())
        session: ReplSession = fake_plain_console[0]["session"]

        assert session.eval_entry("import std/config").ok
        session.reset()

        result = session.eval_entry("import std/config\nstd/config::timeout")
        assert result.ok
        assert isinstance(result.value, RecordValue)
        assert result.value.fields["value"] == TextValue("30s")

    def test_undeclared_exec_keys_warn_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        (home / ".agm").mkdir(parents=True, exist_ok=True)
        (home / ".agm" / "config.toml").write_text(
            '[exec]\ndefault-agent = "old"\ndefault-sandbox = "Native"\necho = true\n'
        )

        repl_command.run(_args())

        err = capsys.readouterr().err
        assert err.count("default-agent") == 1
        assert err.count("default-sandbox") == 1
        assert err.count("echo") == 0

    @pytest.mark.parametrize(
        ("args", "expected_seed", "expected_seconds"),
        [
            (_args(), "30s", 30.0),
            (_args(timeout="5s"), "5s", 5.0),
            (_args(no_timeout=True), None, None),
        ],
    )
    def test_cli_timeout_flags_override_the_configured_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        args: ReplArgs,
        expected_seed: str | None,
        expected_seconds: float | None,
    ) -> None:
        """``--timeout``/``--no-timeout`` set the readable setting and every consumer's timeout."""
        from agnette.agl.semantics.values import RecordValue, TextValue

        home = _isolated_home(monkeypatch, tmp_path)
        (home / ".agm").mkdir()
        (home / ".agm" / "config.toml").write_text('[exec]\ntimeout = "30s"\n')
        idle_timeouts: list[float | None] = []
        real_factory = repl_command.value_driven_agent_factory

        def recording_factory(*, idle_timeout: float | None, **kwargs: object) -> object:
            idle_timeouts.append(idle_timeout)
            return real_factory(idle_timeout=idle_timeout, **kwargs)

        monkeypatch.setattr(repl_command, "value_driven_agent_factory", recording_factory)

        repl_command.run(args)
        session: ReplSession = fake_plain_console[0]["session"]

        result = session.eval_entry("import std/config\nstd/config::timeout")
        assert result.ok
        assert isinstance(result.value, RecordValue)
        if expected_seed is None:
            assert result.value.fields == {}
        else:
            assert result.value.fields["value"] == TextValue(expected_seed)
        assert idle_timeouts == [expected_seconds]
        assert session._shell_exec_timeout == expected_seconds

    def test_exec_config_seeds_each_configured_engine_setting(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir()
        (agm_dir / "config.toml").write_text(
            "[exec]\n"
            "strict-json = true\n"
            'timeout = "2s"\n'
            "trace = true\n"
            'trace-file = "configured.jsonl"\n'
        )
        monkeypatch.setattr(
            repl_command, "prepare_trace_log_from_decision", lambda *args, **kwargs: None
        )

        repl_command.run(_args())

        session: ReplSession = fake_plain_console[0]["session"]
        assert set(session._engine_seed) == {
            "strict-json",
            "timeout",
            "trace",
            "trace-file",
        }

    def test_cli_false_strict_json_over_empty_exec_config_seeds_correctly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.agl.semantics.values import BoolValue

        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir()
        (agm_dir / "config.toml").write_text("[exec]\n")

        repl_command.run(_args(strict_json=False))

        session: ReplSession = fake_plain_console[0]["session"]
        assert session._engine_seed["strict-json"] == BoolValue(False)

    def test_history_path_uses_agm_home_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path / "home")
        agm_home = tmp_path / "relocated-agm"
        monkeypatch.setenv("AGM_HOME", str(agm_home))

        repl_command.run(_args(plain=False))

        call = fake_console[0]
        assert call["history_path"] == agm_home / "repl_history"

    @pytest.mark.parametrize(
        ("args", "expected_trace", "expected_file"),
        [
            (_args(trace=True), True, None),
            (_args(no_trace=True), False, None),
            (_args(trace_file="custom.jsonl"), True, "custom.jsonl"),
        ],
    )
    def test_cli_logging_flags_seed_builtin_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        args: ReplArgs,
        expected_trace: bool,
        expected_file: str | None,
    ) -> None:
        from agnette.agl.semantics.values import BoolValue, RecordValue, TextValue
        from agnette.config.engine_keys import HOST_CONSUMED_ENGINE_KEYS

        _isolated_home(monkeypatch, tmp_path)
        monkeypatch.setattr(
            repl_command, "prepare_trace_log_from_decision", lambda *args, **kwargs: None
        )
        repl_command.run(args)
        session: ReplSession = fake_plain_console[0]["session"]
        host_settings = {
            key: value
            for key, value in session._current.items()
            if key in HOST_CONSUMED_ENGINE_KEYS
        }
        assert host_settings["trace"] == BoolValue(expected_trace)
        if expected_file is None:
            assert "trace-file" not in host_settings
        else:
            trace_file = host_settings["trace-file"]
            assert isinstance(trace_file, RecordValue)
            assert trace_file.fields["value"] == TextValue(expected_file)

    def test_shared_dry_run_state_does_not_affect_repl(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.core import dry_run

        _isolated_home(monkeypatch, tmp_path)
        monkeypatch.setattr(dry_run, "enabled", lambda: True)
        repl_command.run(_args())
        assert len(fake_plain_console) == 1

    def test_quiet_disables_echo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(quiet=True))
        assert fake_plain_console[0]["echo"] is False

    def test_cli_parse_error_retries_seeds_the_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.agl.semantics.values import IntValue

        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(parse_error_retries=5))
        session: ReplSession = fake_plain_console[0]["session"]
        assert session.eval_entry("import std/config").ok
        assert session.eval_entry("config::parse-error-retries").value == IntValue(5)

    def test_negative_parse_error_retries_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args(parse_error_retries=-1))

        assert exc_info.value.code == 1
        assert fake_plain_console == []

    def test_blank_agent_literal_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A blank ``--agent`` is a host-shape error, rejected before the session builds."""
        _isolated_home(monkeypatch, tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args(agent=""))

        assert exc_info.value.code == 1
        assert "'agent'" in capsys.readouterr().err
        assert fake_plain_console == []

    @pytest.mark.parametrize("value", ["true", "(", "worker --flag"])
    def test_non_agent_cli_syntax_opens_with_an_agent_command(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        value: str,
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)

        repl_command.run(_args(agent=value))

        assert len(fake_plain_console) == 1

    def test_non_constant_agent_literal_exits_1_before_the_session_builds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A well-typed but non-constant ``--agent`` literal exits before the banner."""
        _isolated_home(monkeypatch, tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args(agent='AgentCommand("not " + "constant")'))

        assert exc_info.value.code == 1
        assert "--agent" in capsys.readouterr().err
        assert fake_plain_console == []

    def test_malformed_agent_literal_with_no_stdlib_exits_before_the_session_builds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--no-stdlib`` never loads ``std/config``, but a malformed
        ``--agent`` literal still exits 1: the decode failure is
        reported directly, independent of whether any module ever loads."""
        _isolated_home(monkeypatch, tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args(agent='AgentClaude(model = "x"', no_stdlib=True))

        assert exc_info.value.code == 1
        assert "--agent" in capsys.readouterr().err
        assert fake_plain_console == []

    def test_config_agent_with_no_stdlib_opens_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """A project-configured ``[exec] agent`` decodes and seeds the
        session cleanly even when ``--no-stdlib`` means ``std/config`` never
        loads: the seed reaches the interpreter's register independent of the
        module graph."""
        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir()
        agm_dir.joinpath("config.toml").write_text("[exec]\nagent = 'AgentCommand(\"echo cfg\")'\n")

        repl_command.run(_args(no_stdlib=True))

        assert len(fake_plain_console) == 1

    def test_stdlib_version_mismatch_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A stdlib version mismatch from root resolution causes exit 1."""
        from agnette.config.module_roots import StdlibVersionMismatchError

        _isolated_home(monkeypatch, tmp_path)

        def fake_select_stdlib_root(*, home: Path, anchor: Path | None = None) -> StdlibSelection:
            raise StdlibVersionMismatchError("0.0.1", "0.1.0")

        monkeypatch.setattr(repl_command, "select_stdlib_root", fake_select_stdlib_root)

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args())

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "0.0.1" in captured.err
        assert "just install" in captured.err
        assert fake_plain_console == []

    def test_open_diagnostics_from_a_broken_stdlib_exit_1_before_the_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``session.open()`` loading the initial library image can still
        fail -- e.g. a broken standard-library source -- and that exits 1
        before any front end prints its banner."""
        from shutil import copytree

        _isolated_home(monkeypatch, tmp_path)

        stdlib = tmp_path / "broken-stdlib"
        copytree(Path(__file__).resolve().parent.parent / "packages" / "stdlib", stdlib)
        config_agl = stdlib / MODULE_TREE_DIRNAME / "config.agl"
        config_agl.write_text(
            config_agl.read_text(encoding="utf-8") + '\nlet broken: int = "text"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            repl_command,
            "select_stdlib_root",
            lambda **_kwargs: StdlibSelection(stdlib, installed=False),
        )

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args())

        assert exc_info.value.code == 1
        assert capsys.readouterr().err
        assert fake_plain_console == []

    def test_blank_agent_config_literal_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        config_dir = home / ".agm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[exec]\nagent = ""\n')

        with pytest.raises(SystemExit) as exc_info:
            repl_command.run(_args())

        assert exc_info.value.code == 1
        error = capsys.readouterr().err
        assert "'agent'" in error
        assert fake_plain_console == []

    def test_non_agent_config_syntax_opens_with_an_agent_command(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        home = _isolated_home(monkeypatch, tmp_path)
        config_dir = home / ".agm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[exec]\nagent = "not an agent"\n')

        repl_command.run(_args())

        assert len(fake_plain_console) == 1

    def test_invalid_config_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)

        def boom(*_args: object, **_kwargs: object) -> object:
            raise ValueError("bad config")

        monkeypatch.setattr(repl_command, "exec_config_from_merged", boom)
        args = ReplArgs(quiet=False)
        with pytest.raises(SystemExit) as excinfo:
            repl_command.run(args)
        assert excinfo.value.code == 1
        assert fake_plain_console == []

    def test_numeric_timeout_in_config_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """An int-typed [exec].timeout in the TOML config is accepted."""
        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir(parents=True, exist_ok=True)
        (agm_dir / "config.toml").write_text("[exec]\ntimeout = 30\n")
        repl_command.run(_args())
        assert len(fake_plain_console) == 1

    def test_tiny_numeric_timeout_round_trips_through_builtin_setting(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.agl.semantics.values import RecordValue, TextValue

        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir(parents=True, exist_ok=True)
        (agm_dir / "config.toml").write_text("[exec]\ntimeout = 0.0000001\n")

        repl_command.run(_args())
        session = fake_plain_console[0]["session"]
        assert isinstance(session, ReplSession)
        result = session.eval_entry(
            "import std/config\nstd/config::timeout := std/config::timeout\nstd/config::timeout"
        )

        assert result.ok
        assert isinstance(result.value, RecordValue)
        assert result.value.fields["value"] == TextValue("0.0000001s")

    def test_string_timeout_in_config_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """A string-typed [exec].timeout in the TOML config is accepted."""
        home = _isolated_home(monkeypatch, tmp_path)
        agm_dir = home / ".agm"
        agm_dir.mkdir(parents=True, exist_ok=True)
        (agm_dir / "config.toml").write_text('[exec]\ntimeout = "30s"\n')
        repl_command.run(_args())
        assert len(fake_plain_console) == 1


# ---------------------------------------------------------------------------
# Front-end selection: --plain and plain_mode_engaged
# ---------------------------------------------------------------------------


class TestReplFrontEndSelection:
    """``repl.run`` ORs ``args.plain`` with the ``plain_mode_engaged`` predicate.

    Each test monkeypatches ``repl_command.plain_mode_engaged`` directly so the
    outcome never depends on whether the test process itself has a tty.
    """

    def test_plain_flag_forces_plain_even_when_the_predicate_says_console(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        fake_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        # fake_console already forces the predicate False (as if on a tty);
        # --plain must still win.
        repl_command.run(_args(plain=True))

        assert len(fake_plain_console) == 1
        assert fake_console == []

    def test_predicate_true_selects_plain_without_the_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        monkeypatch.setattr(repl_command, "plain_mode_engaged", lambda **_kwargs: True)

        repl_command.run(_args(plain=False))

        assert len(fake_plain_console) == 1

    def test_predicate_false_and_no_flag_selects_console(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_console: list[dict[str, object]],
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)

        repl_command.run(_args(plain=False))

        assert len(fake_console) == 1
        assert fake_plain_console == []

    def test_predicate_receives_the_real_streams_and_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """``repl.run`` passes the live ``sys.stdin``/``sys.stdout``/``os.environ``."""
        _isolated_home(monkeypatch, tmp_path)
        received: dict[str, object] = {}

        def fake_predicate(*, stdin: object, stdout: object, env: object) -> bool:
            received["stdin"] = stdin
            received["stdout"] = stdout
            received["env"] = env
            return True

        monkeypatch.setattr(repl_command, "plain_mode_engaged", fake_predicate)

        repl_command.run(_args(plain=False))

        import os

        assert received["stdin"] is sys.stdin
        assert received["stdout"] is sys.stdout
        assert received["env"] is os.environ


# ---------------------------------------------------------------------------
# Wiring: module roots and trace path resolution
# ---------------------------------------------------------------------------


class TestReplModuleRoots:
    def test_configured_lib_root_wired_into_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """When [modules] lib_root is set in config, it is resolved and passed to the session."""
        home = _isolated_home(monkeypatch, tmp_path)
        # Create an AgM home config with a lib_root pointing to a local dir.
        lib_dir = tmp_path / "mylib"
        lib_dir.mkdir()
        # Put a minimal module in lib_dir so we can verify it is importable.
        (lib_dir / "mymod.agl").write_text('def greet() -> text = "hello"\n', encoding="utf-8")
        agm_home = home / ".agm"
        agm_home.mkdir(parents=True, exist_ok=True)
        (agm_home / "config.toml").write_text(f"[modules]\nlib_root = {str(lib_dir)!r}\n")
        repl_command.run(_args())
        session = fake_plain_console[0]["session"]
        assert isinstance(session, ReplSession)
        # The lib_root is wired: importing a module from lib_dir succeeds.
        result = session.eval_entry("import mymod")
        assert result.ok


class TestReplModuleParameterConfig:
    """The command supplies layered module routes to its incremental session."""

    def _open_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
        *,
        config: str,
        module_source: str,
        modules: Mapping[str, str] | None = None,
    ) -> ReplSession:
        """Open a session over library module ``A/logging`` and the extra *modules*.

        *modules* maps a path below ``A/`` (without ``.agl``) to its source.
        """
        _isolated_home(monkeypatch, tmp_path / "home")
        project = tmp_path / "project"
        config_dir = project / "config"
        library = tmp_path / "library"
        config_dir.mkdir(parents=True)
        (library / "A").mkdir(parents=True)
        (library / "A" / "logging.agl").write_text(module_source, encoding="utf-8")
        for name, source in (modules or {}).items():
            path = library / "A" / f"{name}.agl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        (config_dir / "config.toml").write_text(
            '[modules]\nroots = ["../../library"]\n\n' + config,
            encoding="utf-8",
        )
        monkeypatch.setenv("PROJ_DIR", str(project))
        monkeypatch.chdir(project)

        repl_command.run(_args(no_stdlib=True))

        session = fake_plain_console[0]["session"]
        assert isinstance(session, ReplSession)
        return session

    def test_module_table_seeds_an_imported_parameter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging]\nverbose = true\n",
            module_source="@param let verbose: bool = false\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::verbose")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value is True

    def test_scope_region_table_seeds_an_imported_parameter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.debug]\ntrace = true\n",
            module_source="scope debug\n  @param let trace: bool = false\nend debug\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::debug::trace")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value is True

    def test_program_table_does_not_override_a_module_route(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config=("[A.logging]\nverbose = false\n\n[workflow.run]\nverbose = true\n"),
            module_source="@param let verbose: bool = true\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::verbose")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value is False

    def test_ambiguous_module_suffix_across_new_imports_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[logging]\nverbose = true\n",
            module_source="@param let verbose: bool = false\n",
        )
        second = tmp_path / "library" / "B"
        second.mkdir()
        (second / "logging.agl").write_text("@param let verbose: bool = false\n", encoding="utf-8")

        result = session.eval_entry("import A/logging\nimport B/logging\n()")

        assert not result.ok
        assert any("multiple routes" in diagnostic.message for diagnostic in result.diagnostics)

    def test_importing_a_module_twice_resolves_its_config_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        calls: list[tuple[HostParamKey, ...]] = []
        resolve = repl_command.resolve_declaration_route_values

        def record_resolver(
            config: GeneralConfig,
            params: Sequence[HostParamInfo],
            nested_routes: frozenset[tuple[str, ...]],
        ) -> dict[HostParamKey, object]:
            calls.append(tuple(param.key for param in params))
            return resolve(config, params, nested_routes)

        monkeypatch.setattr(repl_command, "resolve_declaration_route_values", record_resolver)
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging]\nvalue = 7\n",
            module_source="@param let value: int = 1\n",
        )
        first = session.eval_entry("import A/logging\nA/logging::value")
        second = session.eval_entry("import A/logging\nA/logging::value")

        assert first.ok, first.diagnostics
        assert second.ok, second.diagnostics
        assert first.value is not None
        assert first.value.value == 7
        assert second.value is not None
        assert second.value.value == 7
        assert len(calls) == 1

    def test_function_route_seeds_an_imported_function_parameter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.refine]\nsteps = 7\n",
            module_source="def refine(@param steps: int = 1) -> int = steps\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::refine()")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 7

    def test_an_explicit_argument_beats_a_function_route_seed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.refine]\nsteps = 7\n",
            module_source="def refine(@param steps: int = 1) -> int = steps\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::refine(2)")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 2

    def test_a_function_seed_persists_into_later_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.refine]\nsteps = 7\n",
            module_source="def refine(@param steps: int = 1) -> int = steps\n",
        )

        assert session.eval_entry("import A/logging").ok
        result = session.eval_entry("A/logging::refine()")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 7

    def test_a_function_parameter_keeps_its_default_without_a_function_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging]\nsteps = 9\n",
            module_source="def refine(@param steps: int = 1) -> int = steps\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::refine()")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 1

    def test_a_malformed_function_route_value_fails_the_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config='[A.logging.refine]\nsteps = "many"\n',
            module_source="def refine(@param steps: int = 1) -> int = steps\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::refine()")

        assert not result.ok

    def test_a_scoped_function_route_seeds_an_imported_function_parameter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.Sc.deep]\ndepth = 7\n",
            module_source="scope Sc\n  def deep(@param depth: int = 1) -> int = depth\nend Sc\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::Sc::deep()")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 7

    def test_a_method_route_seeds_an_imported_method_parameter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.Counter.bump]\nby = 7\n",
            module_source=(
                "record Counter\n  start: int\n\n"
                "def Counter::bump(self, @param by: int = 1) -> int = self.start + by\n"
            ),
        )

        result = session.eval_entry("import A/logging\nA/logging::Counter(start = 10).bump()")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 17

    @pytest.mark.parametrize(
        ("function_module", "sibling", "sibling_module", "config", "entries"),
        [
            pytest.param(
                "def refine(@param steps: int = 1) -> int = steps\n",
                "refine",
                "@param let steps: int = 0\n",
                "[A.logging.refine]\nsteps = 7\n",
                ["import A/logging", "import A/logging/refine"],
                id="function-route-later",
            ),
            pytest.param(
                "def refine(@param steps: int = 1) -> int = steps\n",
                "refine",
                "@param let steps: int = 0\n",
                "[A.logging.refine]\nsteps = 7\n",
                ["import A/logging/refine", "import A/logging"],
                id="function-route-earlier",
            ),
            pytest.param(
                "def refine(@param steps: int = 1) -> int = steps\n",
                "refine",
                "@param let steps: int = 0\n",
                "[A.logging.refine]\nsteps = 7\n",
                ["import A/logging\nimport A/logging/refine"],
                id="function-route-same-entry",
            ),
            pytest.param(
                "def refine(@param steps: int = 1) -> int = steps\n",
                "refine",
                "@param let steps: int = 0\n",
                "",
                ["import A/logging", "import A/logging/refine"],
                id="function-route-without-config",
            ),
            pytest.param(
                "scope debug\n  @param let trace: bool = false\nend debug\n",
                "debug",
                "@param let trace: bool = false\n",
                "[A.logging.debug]\ntrace = true\n",
                ["import A/logging", "import A/logging/debug"],
                id="scope-route",
            ),
        ],
    )
    def test_colliding_declaration_routes_fail_the_entry(
        self,
        function_module: str,
        sibling: str,
        sibling_module: str,
        config: str,
        entries: list[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config=config,
            module_source=function_module,
            modules={f"logging/{sibling}": sibling_module},
        )

        results = [session.eval_entry(entry) for entry in entries]

        assert [result.ok for result in results] == [True] * (len(entries) - 1) + [False]

    def test_a_failed_collision_entry_leaves_the_session_usable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.refine]\nsteps = 7\n",
            module_source="def refine(@param steps: int = 1) -> int = steps\n",
            modules={"logging/refine": "@param let steps: int = 0\n"},
        )
        assert session.eval_entry("import A/logging").ok
        assert not session.eval_entry("import A/logging/refine").ok

        result = session.eval_entry("A/logging::refine()")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 7

    def test_a_route_table_of_an_earlier_module_is_no_value_of_a_later_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        """``[A.logging.steps]`` is the route of module ``A/logging/steps``, not a value."""
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="[A.logging.steps]\nfoo = 5\n",
            module_source="@param let steps: int = 1\n",
            modules={"logging/steps": "@param let foo: int = 0\n"},
        )
        assert session.eval_entry("import A/logging/steps").ok

        result = session.eval_entry("import A/logging\nA/logging::steps")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 1

    def test_initializer_applies_without_a_module_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        session = self._open_session(
            monkeypatch,
            tmp_path,
            fake_plain_console,
            config="",
            module_source="@param let value: int = 3\n",
        )

        result = session.eval_entry("import A/logging\nA/logging::value")

        assert result.ok, result.diagnostics
        assert result.value is not None
        assert result.value.value == 3


class TestReplTrace:
    def test_trace_file_threaded_into_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        trace_file = tmp_path / "trace.log"
        repl_command.run(_args(trace_file=str(trace_file)))
        # The validate-up-front touch creates the (empty) file.
        assert trace_file.exists()
        session = fake_plain_console[0]["session"]
        assert isinstance(session, ReplSession)

    def test_no_trace_writes_no_trace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        repl_command.run(_args(no_trace=True))
        # Nothing under .agent-files was created for a --no-trace session.
        assert not (tmp_path / ".agent-files").exists()

    def test_shared_dry_run_state_does_not_suppress_trace_setup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        from agnette.core import dry_run

        _isolated_home(monkeypatch, tmp_path)
        monkeypatch.setattr(dry_run, "enabled", lambda: True)
        trace_file = tmp_path / "trace.log"

        prepared: list[bool] = []

        def prepare_trace(*args: object, **kwargs: object) -> Path:
            prepared.append(True)
            return trace_file

        monkeypatch.setattr(repl_command, "prepare_trace_log_from_decision", prepare_trace)
        repl_command.run(_args(trace_file=str(trace_file)))
        assert prepared == [True]

    def test_unwritable_trace_file_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_plain_console: list[dict[str, object]],
    ) -> None:
        _isolated_home(monkeypatch, tmp_path)
        # A path whose parent is a regular file cannot be created (mkdir fails).
        not_a_dir = tmp_path / "afile"
        not_a_dir.write_text("x")
        trace_file = not_a_dir / "trace.log"
        with pytest.raises(SystemExit) as excinfo:
            repl_command.run(_args(trace_file=str(trace_file)))
        assert excinfo.value.code == 1
        assert fake_plain_console == []
