"""Implementation of the ``agm repl`` command.

Launches an interactive read-eval-print loop for the AgL workflow language.
The REPL shares ``agm exec``'s ``[exec]`` configuration and its engine-setting
flags, resolved as host parameters through the same resolver (with no program),
so an interactive session evaluates entries with the same agent dispatch backing
a batch ``agm exec`` run would use.

The command itself is thin: it resolves configuration the same way ``exec``
does, builds a value-driven dispatcher, constructs a :class:`ReplSession`,
then hands control to one of two front ends sharing the same UI-free loop
core (:mod:`agnette.agl.repl.loop`):
:func:`agnette.agl.repl.console.run_console` (prompt_toolkit) or
:func:`agnette.agl.repl.plain_console.run_plain_console` (plain line I/O, for a
pipe, comint buffer, or any other non-terminal consumer). The front end is
chosen by :func:`~agnette.agl.repl.plain_console.plain_mode_engaged` (non-tty
stdin/stdout, or ``TERM=dumb``) or by the explicit ``--plain`` flag; there is
no flag to force prompt_toolkit onto a pipe. All the interactive logic lives
in :mod:`agnette.agl.repl`.

Each REPL entry and its loaded library modules receive ``std/prelude``
glob imports by default. An explicit import whose expansion includes
``std/prelude`` supplies that contribution instead, so plain ``import std/prelude``
leaves prelude names qualified-only. ``--no-stdlib`` disables the automatic import
throughout every loaded REPL program. Imports are qualified by default; tails
and ``use`` declarations opt into bare names.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Collection, Mapping, Sequence
from functools import partial

from agnette.agent.session.service import create_agl_session_host
from agnette.agl.diagnostics import AglError, format_diagnostic
from agnette.agl.ir.contracts import ParamDecoder
from agnette.agl.ir.static_keys import HostParamKey
from agnette.agl.repl import ReplSession
from agnette.agl.repl.plain_console import plain_mode_engaged
from agnette.agl.runtime.agents import value_driven_agent_factory
from agnette.agl.runtime.engine_config import EngineSettings
from agnette.agl.runtime.host_settings import HostSettingsPolicy
from agnette.agl.runtime.types import HostParamInfo
from agnette.agl.type_schema import engine_setting_decoders
from agnette.agl.warning_policy import WarningPolicy
from agnette.agm.cli_support.agl_errors import fail_with_diagnostics
from agnette.agm.cli_support.args import ReplArgs
from agnette.agm.cli_support.engine_seeds import (
    engine_seed_values,
    host_agent_sandbox_resolver,
    host_agent_spec_resolver,
    parse_engine_flags,
    resolve_strict_json,
    resolve_timeout,
    trace_decision,
)
from agnette.agm.cli_support.program_options import CommandUsageError
from agnette.agm.parser import exit_with_usage_error
from agnette.config.claims import QualifiedConfigLookupError
from agnette.config.context import current_config_context
from agnette.config.engine_keys import DECLARATION_VALUED_ENGINE_KEYS, ENGINE_KEY_NAMES
from agnette.config.general import (
    GeneralConfig,
    exec_options_from_merged,
    load_general_config,
    load_repl_config,
    save_repl_setting,
)
from agnette.config.home import agm_home_dir
from agnette.config.module_roots import (
    StdlibResolutionError,
    load_module_roots,
    resolve_lib_root,
    select_stdlib_root,
)
from agnette.core.cleanup import preserve_primary_error
from agnette.core.log import LiveTracePathResolver, prepare_trace_log_from_decision
from agnette.core.process import terminating_signals_exit
from agnette.host_params.config import (
    ProgramlessEngineValues,
    colliding_declarations,
    declaration_route_tables,
    programless_engine_values,
    resolve_declaration_route_values,
)
from agnette.packages.activation import select_package_roots
from agnette.packages.development import discover_development_packages
from agnette.packages.store import context_layers
from agnette.sandbox.prepare import lazy_sandbox_context


def _resolve_param_seeds(
    config: GeneralConfig,
    new: Sequence[HostParamInfo],
    loaded: Sequence[HostParamInfo],
) -> Mapping[HostParamKey, object]:
    """Resolve the *new* parameters' declaration-route seeds, as ``agm exec`` does.

    A spelling shared with any *loaded* parameter, an ambiguous config key, or
    a malformed value is an ``AglError``; *loaded* routes are nested routes.
    """
    collision = colliding_declarations((*loaded, *new))
    if collision is not None:
        raise AglError(collision)
    try:
        return resolve_declaration_route_values(config, new, declaration_route_tables(loaded))
    except QualifiedConfigLookupError as exc:
        raise AglError(str(exc)) from exc


def _engine_values(general_config: GeneralConfig, tokens: Sequence[str]) -> ProgramlessEngineValues:
    """Read the raw engine settings from *tokens* over ``[exec]``.

    Raises :class:`CommandUsageError` for a malformed flag.
    """
    try:
        supplied = parse_engine_flags(tokens)
    except ValueError as exc:
        raise CommandUsageError(str(exc)) from exc
    return programless_engine_values(general_config, supplied)


def _decode_engine_settings(
    values: ProgramlessEngineValues, decoders: Mapping[str, ParamDecoder], names: Collection[str]
) -> EngineSettings:
    """Decode the engine settings *names* of *values*; an invalid one fails the command.

    An invalid command-line value is a usage error; an invalid configured one exits 1.
    """
    settings, diagnostics, usage = values.resolve(decoders, names)
    if diagnostics:
        try:
            fail_with_diagnostics(diagnostics, usage)
        except CommandUsageError as exc:
            exit_with_usage_error(["repl"], f"error: {exc.message}")
    return settings


def run(args: ReplArgs, *, warning_policy: WarningPolicy | None = None) -> None:
    """Run the ``agm repl`` command; every entry is governed by *warning_policy*."""
    ctx = current_config_context()
    try:
        general_config = load_general_config(home=ctx.home, proj_dir=ctx.proj_dir, cwd=ctx.cwd)
        merged_config = general_config.merged
        options = exec_options_from_merged(merged_config)
    except ValueError as exc:
        print(f"Error: invalid exec configuration: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    resolve_agent_spec = host_agent_spec_resolver(merged_config)
    repl_config = load_repl_config(home=ctx.home, proj_dir=ctx.proj_dir, cwd=ctx.cwd)

    # Resolve max call depth: CLI > [exec] config (config pragmas are not applied
    # in the REPL).  ``None`` lets the session apply the canonical default.
    call_depth_limit = (
        args.max_call_depth if args.max_call_depth is not None else options.max_call_depth
    )

    # Seed only explicit CLI/config controls.  Trace-service fallbacks remain
    # absent so a ``builtin var`` initializer can provide the setting default.
    try:
        engine_values = _engine_values(general_config, args.tokens)
    except CommandUsageError as exc:
        exit_with_usage_error(["repl"], f"error: {exc.message}")
    # The settings naming builtin declarations' members decode once the library image
    # loads them (below); the rest configure the session itself.
    engine_settings = _decode_engine_settings(
        engine_values,
        engine_setting_decoders({}),
        ENGINE_KEY_NAMES - DECLARATION_VALUED_ENGINE_KEYS,
    )
    engine_seeds = engine_seed_values(engine_settings)
    timeout = resolve_timeout(engine_seeds)
    strict_json = resolve_strict_json(engine_seeds)
    echo_agent_output = options.echo if args.echo is None else args.echo

    # One resolution for both the readable ``trace`` seed and the trace file
    # prepared below, exactly as ``agm exec`` does.
    decision = trace_decision(engine_settings)

    # Resolve and validate the trace log file up front so an unwritable
    # ``--trace-file`` exits 1 BEFORE the loop starts rather than crashing
    # mid-session.
    trace_path = prepare_trace_log_from_decision(decision, command_name="repl")

    # Built once and shared by every sandboxing consumer this session
    # dispatches through, so they all resolve sandbox configuration from the
    # same `SandboxContext` and the `[run.*]` config loads at most once.
    get_sandbox_context = lazy_sandbox_context(ctx)
    runner_agent = value_driven_agent_factory(
        idle_timeout=timeout, get_sandbox_context=get_sandbox_context
    )
    session_host = create_agl_session_host(
        idle_timeout=timeout, get_sandbox_context=get_sandbox_context
    )

    host_settings_policy = HostSettingsPolicy(
        resolve_trace_path=LiveTracePathResolver(command_name="repl", auto_path=trace_path),
    )

    mod_roots_cfg = load_module_roots(home=ctx.home, proj_dir=ctx.proj_dir, cwd=ctx.cwd)
    try:
        stdlib = select_stdlib_root(home=ctx.home, anchor=ctx.cwd)
    except StdlibResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    lib_root = resolve_lib_root(mod_roots_cfg, home=ctx.home)
    try:
        stores = context_layers(home=ctx.home, proj_dir=ctx.proj_dir)
        package_roots = select_package_roots(
            home=ctx.home,
            proj_dir=ctx.proj_dir,
            cwd=ctx.cwd,
            development_packages=discover_development_packages(stores, ctx.cwd),
        )
    except ValueError as exc:
        print(f"Error: invalid package roots: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    process_environment = dict(os.environ)
    with terminating_signals_exit():
        session = ReplSession(
            default_strict_json=strict_json,
            default_call_depth_limit=call_depth_limit,
            agent_dispatcher=runner_agent,
            session_host=session_host,
            get_sandbox_context=get_sandbox_context,
            resolve_agent_spec=resolve_agent_spec,
            shell_exec_timeout=timeout,
            trace_path=trace_path,
            echo_agent_output=echo_agent_output,
            engine_base=engine_seeds,
            process_environment=process_environment,
            host_settings_policy=host_settings_policy,
            cwd=ctx.cwd,
            stdlib_root=stdlib.root,
            stdlib_installed=stdlib.installed,
            lib_root=lib_root,
            configured_roots=mod_roots_cfg.extra,
            package_roots=package_roots,
            default_stdlib=not args.no_stdlib,
            param_seed_resolver=partial(_resolve_param_seeds, general_config),
            warning_policy=warning_policy,
        )

        with preserve_primary_error(session.close, label="session cleanup"):
            # Load and check the session's initial library image now, so any
            # startup failure loading the standard library exits before the
            # console opens and prints its banner, rather than surfacing only
            # once the first entry runs.
            open_diagnostics = session.open()
            if open_diagnostics:
                for diagnostic in open_diagnostics:
                    print(f"Error: {format_diagnostic(diagnostic)}", file=sys.stderr)
                raise SystemExit(1)
            decoders = session.engine_setting_decoders()
            declaration_valued = _decode_engine_settings(
                engine_values, decoders, DECLARATION_VALUED_ENGINE_KEYS
            )
            session.seed_engine_settings(
                declaration_valued.values,
                resolve_agent_sandbox=host_agent_sandbox_resolver(
                    merged_config, declaration_valued, decoders
                ),
            )

            history_path = agm_home_dir(home=ctx.home) / "repl_history"
            history_path.parent.mkdir(parents=True, exist_ok=True)

            def on_setting_save(key: str, value: str | bool) -> None:
                save_repl_setting(key, value, home=ctx.home)

            # ``--quiet`` disables echo for this session only and never persists,
            # overriding a saved ``[repl] echo = true``; absent ``--quiet`` the
            # persisted (or default) ``[repl] echo`` applies.
            echo = repl_config.echo and not args.quiet

            # The front end is chosen once, here: ``--plain`` forces the plain line
            # front end; otherwise ``plain_mode_engaged`` auto-detects it from
            # stdin/stdout (a pipe, redirected file, or a dumb terminal). There is no
            # flag to force prompt_toolkit onto a non-terminal. The ``console`` import
            # stays local so a plain session never pulls in prompt_toolkit; the
            # ``plain_console`` import is local too for symmetry and late binding —
            # ``plain_mode_engaged`` is already imported from it at module top, so
            # this local import defers nothing on the plain branch, but the late
            # binding is what test fixtures rely on when they monkeypatch it.
            if args.plain or plain_mode_engaged(stdin=sys.stdin, stdout=sys.stdout, env=os.environ):
                from agnette.agl.repl.plain_console import run_plain_console

                run_plain_console(
                    session,
                    echo=echo,
                    echo_unit=repl_config.echo_unit,
                    theme=repl_config.theme,
                    on_setting_save=on_setting_save,
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                )
                return

            from agnette.agl.repl.console import run_console

            run_console(
                session,
                echo=echo,
                echo_unit=repl_config.echo_unit,
                history_path=history_path,
                theme=repl_config.theme,
                on_setting_save=on_setting_save,
            )
