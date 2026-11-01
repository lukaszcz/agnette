"""The external spellings a field or record declaration carries."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["NO_EXTERNAL_NAME", "ExternalName"]


@dataclass(frozen=True, slots=True)
class ExternalName:
    """The ``@name`` and ``@json-name`` arguments of one field or record declaration.

    ``name`` is the first value-syntax alias and JSON default; ``extra_names``
    holds further aliases, and ``json_name`` overrides the JSON spelling.
    """

    name: str | None = None
    json_name: str | None = None
    extra_names: tuple[str, ...] = ()

    def json(self, declared: str) -> str:
        """Return the JSON spelling of a declaration named *declared*."""
        if self.json_name is not None:
            return self.json_name
        return declared if self.name is None else self.name

    def value_names(self, declared: str) -> tuple[str, ...]:
        """Return every value-syntax spelling of a declaration named *declared*."""
        names: list[str] = [declared]
        for name in (self.name, *self.extra_names):
            if name is not None and name not in names:
                names.append(name)
        return tuple(names)

    def alias(self, declared: str) -> str | None:
        """Return the first alias differing from *declared*, or ``None``.

        The single rule for a decode descriptor's optional alias field:
        derived from :meth:`value_names` so every caller (record, enum member,
        and field descriptors alike) computes it the same way.
        """
        names = self.value_names(declared)
        return names[1] if len(names) > 1 else None


#: The external spellings of a declaration carrying neither attribute.
NO_EXTERNAL_NAME = ExternalName()
