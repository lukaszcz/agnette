"""Companion contracts for the ``std/time`` and ``std/random`` modules."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from types import ModuleType, SimpleNamespace
from typing import Protocol, cast
from uuid import UUID

import pytest

from agnette.agl.eval.ir_interpreter import IrInterpreter
from agnette.agl.ir.ids import NominalId, SymbolId
from agnette.agl.ir.program import (
    ExecutableProgram,
    NominalDescriptor,
    NominalKind,
    ValueDescriptors,
)
from agnette.agl.modules.ids import ENTRY_ID, ModuleId
from agnette.agl.runtime.boundary import AglArrayView, AglException
from agnette.agl.runtime.externs import ExternRegistry
from agnette.agl.runtime.host_session import HostSession
from agnette.agl.runtime.sessions import AgentDispatcherSessionHost
from agnette.agl.semantics.values import ArrayValue, IntValue, TextValue
from tests._agl_helpers import option_nominal_descriptors
from tests.agl.ir_harness import lower_ir

_STDLIB_ROOT = Path(__file__).resolve().parents[1] / "packages" / "stdlib"
_TIME_MODULE = ModuleId(("std", "time"))
_RANDOM_MODULE = ModuleId(("std", "random"))
_TIME_PARSE_ERROR = NominalId(9_600_001)
_INDEX_ERROR = NominalId(9_600_002)
_OPTION = NominalId(9_600_003)
_OPTION_NONE = NominalId(9_600_004)
_OPTION_SOME = NominalId(9_600_005)
_NO_DESCRIPTORS = ValueDescriptors(nominals={}, functions={}, exception_field_encodes={})


class _TimeCompanion(Protocol):
    def now(self) -> Decimal: ...

    def now_iso(self) -> str: ...

    def monotonic(self) -> Decimal: ...

    def sleep(self, seconds: Decimal) -> None: ...

    def parse_iso(self, raw: str) -> Decimal: ...

    def format_iso(self, epoch: Decimal) -> str: ...

    def parse(self, raw: str, fmt: str) -> Decimal: ...

    def format(self, epoch: Decimal, fmt: str) -> str: ...


class _RandomCompanion(Protocol):
    def uuid(self) -> str: ...

    def seed(self, value: int) -> None: ...

    def below(self, upper: int) -> int: ...

    def between(self, lower: int, upper: int) -> int: ...

    def uniform(self) -> Decimal: ...

    def choice(self, values: object) -> object: ...

    def shuffle_in_place(self, values: object) -> None: ...


class _StateCompanion(Protocol):
    barrier: Barrier | None
    results: dict[int, tuple[int, int]]


def _companion(
    module_id: ModuleId, name: str, registry: ExternRegistry | None = None
) -> ModuleType:
    active_registry = registry if registry is not None else ExternRegistry()
    active_registry.set_nominals(
        {
            _TIME_PARSE_ERROR: NominalDescriptor(
                nominal=_TIME_PARSE_ERROR,
                module_id=_TIME_MODULE,
                scope_path=(),
                declared_name="TimeParseError",
                kind=NominalKind.EXCEPTION,
                fields=("message", "raw"),
                field_json_names=("message", "raw"),
            ),
            _INDEX_ERROR: NominalDescriptor(
                nominal=_INDEX_ERROR,
                module_id=ModuleId(("std", "errors")),
                scope_path=(),
                declared_name="IndexError",
                kind=NominalKind.EXCEPTION,
                fields=("message", "index", "length"),
                field_json_names=("message", "index", "length"),
            ),
            **option_nominal_descriptors(_OPTION, _OPTION_NONE, _OPTION_SOME),
        },
    )
    return active_registry.load_companion(module_id, _STDLIB_ROOT / "src" / f"{name}.py")


def test_time_clocks_use_distinct_nanosecond_sources_without_float_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companion = cast(_TimeCompanion, _companion(_TIME_MODULE, "time"))
    monkeypatch.setattr(
        companion,
        "time",
        SimpleNamespace(
            time_ns=lambda: 1_704_164_645_123_456_789,
            monotonic_ns=lambda: 987_654_321_012_345,
        ),
    )

    assert companion.now() == Decimal("1704164645.123456789")
    assert companion.monotonic() == Decimal("987654.321012345")


def test_time_now_iso_preserves_the_current_utc_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    companion = cast(_TimeCompanion, _companion(_TIME_MODULE, "time"))
    instant = datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)

    def now(tz: tzinfo | None = None) -> datetime:
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(companion, "datetime", SimpleNamespace(now=now))

    assert companion.now_iso() == "2024-01-02T03:04:05.123456+00:00"


def test_time_sleep_forwards_each_requested_duration_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companion = cast(_TimeCompanion, _companion(_TIME_MODULE, "time"))
    durations: list[float] = []
    monkeypatch.setattr(companion, "time", SimpleNamespace(sleep=durations.append))

    for seconds in (Decimal(0), Decimal("0.125"), Decimal("2.5")):
        assert companion.sleep(seconds) is None

    assert durations == [0.0, 0.125, 2.5]
    assert all(isinstance(seconds, float) for seconds in durations)


def test_random_uuid_propagates_fresh_entropy_source_values_despite_reseeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companion = cast(_RandomCompanion, _companion(_RANDOM_MODULE, "random"))
    expected = (
        "12345678-1234-4abc-8def-123456789abc",
        "87654321-4321-4fed-9cba-cba987654321",
    )
    generated = iter(UUID(value) for value in expected)
    monkeypatch.setattr(companion, "uuid_module", SimpleNamespace(uuid4=lambda: next(generated)))

    companion.seed(713)
    first = companion.uuid()
    companion.seed(713)
    second = companion.uuid()

    assert (first, second) == expected


def test_time_round_trips_fixed_utc_iso_and_strptime_values() -> None:
    companion = cast(_TimeCompanion, _companion(_TIME_MODULE, "time"))
    epoch = Decimal("1704164645.123456")

    assert companion.format_iso(epoch) == "2024-01-02T03:04:05.123456+00:00"
    assert companion.parse_iso("2024-01-02T03:04:05.123456Z") == epoch
    assert companion.parse_iso("2024-01-02T04:34:05.123456+01:30") == epoch
    assert companion.parse_iso("2024-01-02T03:04:05.123456") == epoch
    assert companion.format(epoch, "%Y/%m/%d %H:%M:%S.%f %z") == "2024/01/02 03:04:05.123456 +0000"
    assert companion.parse("2024/01/02 03:04:05.123456 +0000", "%Y/%m/%d %H:%M:%S.%f %z") == epoch
    assert companion.parse("2024/01/02 04:34:05.123456 +0130", "%Y/%m/%d %H:%M:%S.%f %z") == epoch
    assert companion.parse("2024/01/02 03:04:05.123456", "%Y/%m/%d %H:%M:%S.%f") == epoch


def test_time_parse_errors_are_typed_and_retain_the_raw_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companion = cast(_TimeCompanion, _companion(_TIME_MODULE, "time"))

    for parse, args in ((companion.parse_iso, ("bad",)), (companion.parse, ("bad", "%Y-%m-%d"))):
        with pytest.raises(AglException) as exc_info:
            parse(*args)
        assert exc_info.value.value.nominal == _TIME_PARSE_ERROR
        assert exc_info.value.value.fields["raw"] == TextValue("bad")

    def overflow(*_: object) -> object:
        raise OverflowError

    for name, args in (("parse_iso", ("out of range",)), ("parse", ("out of range", "%Y-%m-%d"))):
        datetime_method = "fromisoformat" if name == "parse_iso" else "strptime"
        monkeypatch.setattr(companion, "datetime", SimpleNamespace(**{datetime_method: overflow}))
        with pytest.raises(AglException) as exc_info:
            getattr(companion, name)(*args)
        assert exc_info.value.value.nominal == _TIME_PARSE_ERROR
        assert exc_info.value.value.fields["raw"] == TextValue("out of range")


def test_random_seed_reproduces_bounded_sequences_and_shuffle_preserves_a_live_multiset() -> None:
    companion = cast(_RandomCompanion, _companion(_RANDOM_MODULE, "random"))
    values = ArrayValue([IntValue(1), IntValue(2), IntValue(2), IntValue(3), IntValue(4)])
    view = AglArrayView(values, _NO_DESCRIPTORS)
    companion.seed(21)
    sequence = (companion.below(100), companion.between(-10, 10), companion.uniform())
    companion.seed(21)
    assert (companion.below(100), companion.between(-10, 10), companion.uniform()) == sequence
    assert 0 <= sequence[0] < 100
    assert -10 <= sequence[1] <= 10
    assert Decimal(0) <= sequence[2] < Decimal(1)

    companion.seed(21)
    companion.shuffle_in_place(view)
    assert sorted(value.value for value in values.elements) == [1, 2, 2, 3, 4]


def test_random_choice_raises_typed_index_error_for_an_empty_array() -> None:
    companion = cast(_RandomCompanion, _companion(_RANDOM_MODULE, "random"))

    with pytest.raises(AglException) as exc_info:
        companion.choice([])

    assert exc_info.value.value.nominal == _INDEX_ERROR
    assert exc_info.value.value.fields["index"] == IntValue(0)
    assert exc_info.value.value.fields["length"] == IntValue(0)


def test_random_choice_returns_an_array_element() -> None:
    companion = cast(_RandomCompanion, _companion(_RANDOM_MODULE, "random"))
    companion.seed(9)

    assert companion.choice(["one", "two", "three"]) in {"one", "two", "three"}


@dataclass(frozen=True)
class _LoadedProgram:
    """An AgL entry lowered with ``std/random`` and its own Python companion loaded."""

    executable: ExecutableProgram
    registry: ExternRegistry
    companion: ModuleType
    program_symbol: SymbolId

    def run(self, *arguments: IntValue, host: HostSession | None = None) -> None:
        IrInterpreter(self.executable, extern_registry=self.registry, host=host).run(
            program_symbol=self.program_symbol, arguments=arguments
        )


def _load_program(tmp_path: Path, source: str, companion_source: str) -> _LoadedProgram:
    entry_path = tmp_path / "entry.agl"
    entry_path.write_text(source)
    entry_path.with_suffix(".py").write_text(companion_source)
    executable = lower_ir(source, origin_path=entry_path)
    registry = ExternRegistry()
    registry.set_nominals(executable.nominals)
    registry.load_companion(_RANDOM_MODULE, _STDLIB_ROOT / "src" / "random.py")
    companion = registry.load_companion(ENTRY_ID, entry_path.with_suffix(".py"))
    return _LoadedProgram(executable, registry, companion, next(iter(executable.program_functions)))


def test_random_state_is_isolated_between_concurrent_real_interpreters(tmp_path: Path) -> None:
    program = _load_program(
        tmp_path,
        "import std/random\n"
        "extern def checkpoint() -> unit\n"
        "extern def report(seed: int, first: int, second: int) -> unit\n"
        "program def main(run-seed: int) -> unit =\n"
        "  random::seed(run-seed)\n"
        "  checkpoint()\n"
        "  let first = random::below(1000000)\n"
        "  let second = random::below(1000000)\n"
        "  report(run-seed, first, second)\n",
        "from threading import Barrier\n"
        "barrier = None\n"
        "results = {}\n"
        "def checkpoint():\n"
        "    if barrier is not None:\n"
        "        barrier.wait(timeout=5)\n"
        "def report(seed, first, second):\n"
        "    results[seed] = (first, second)\n",
    )
    state_companion = cast(_StateCompanion, program.companion)

    def run(seed: int) -> tuple[int, int]:
        program.run(IntValue(seed))
        return state_companion.results.pop(seed)

    expected = {seed: run(seed) for seed in (713, 91)}
    state_companion.barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        actual = dict(zip((713, 91), executor.map(run, (713, 91))))

    assert actual == expected
    assert all(0 <= value < 1_000_000 for sequence in actual.values() for value in sequence)


class _DrawCompanion(Protocol):
    draws: list[tuple[int, int]]


class _ForkDrawRunner:
    """Runs a draw program against one host session, as a (forked) process would."""

    def __init__(self, program: _LoadedProgram) -> None:
        self._program = program
        self.host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)

    def seed(self, value: int) -> None:
        self._program.run(IntValue(1), IntValue(value), host=self.host)

    def draw(self) -> tuple[int, int]:
        self._program.run(IntValue(2), IntValue(0), host=self.host)
        return cast(_DrawCompanion, self._program.companion).draws.pop()


def _load_draw_program(tmp_path: Path) -> _LoadedProgram:
    return _load_program(
        tmp_path,
        "import std/random\n"
        "extern def report(first: int, second: int) -> unit\n"
        "program def main(mode: int, run-seed: int) -> unit =\n"
        "  case mode of\n"
        "    | 1 => random::seed(run-seed)\n"
        "    | _ => report(random::below(1000000000), random::below(1000000000))\n",
        "draws = []\n\ndef report(first, second):\n    draws.append((first, second))\n",
    )


def _draw_after_forks(program: _LoadedProgram, *, forks: int, in_child: bool) -> tuple[int, int]:
    """Draw from a seeded process state after *forks* forks, in the parent or the last child.

    Each call builds its own state: a child starts from the parent's state as of its fork.
    """
    runner = _ForkDrawRunner(program)
    runner.seed(7)
    forked = [runner.host.state.prepare_fork() for _ in range(forks)]
    if in_child:
        runner.host.state.enter_forked_child(forked[-1])
    return runner.draw()


def _forked_draws(program: _LoadedProgram) -> tuple[tuple[int, int], ...]:
    """Draws of the parent after two forks, of the first child, and of the second child."""
    return (
        _draw_after_forks(program, forks=2, in_child=False),
        _draw_after_forks(program, forks=1, in_child=True),
        _draw_after_forks(program, forks=2, in_child=True),
    )


def test_random_stream_is_forked_into_distinct_seed_deterministic_child_streams(
    tmp_path: Path,
) -> None:
    program = _load_draw_program(tmp_path)
    parent, child_a, child_b = _forked_draws(program)

    assert len({parent, child_a, child_b}) == 3
    assert _forked_draws(program) == (parent, child_a, child_b)


def test_random_parent_stream_advances_per_fork(tmp_path: Path) -> None:
    program = _load_draw_program(tmp_path)

    assert (
        len({_draw_after_forks(program, forks=forks, in_child=False) for forks in (0, 1, 2)}) == 3
    )
