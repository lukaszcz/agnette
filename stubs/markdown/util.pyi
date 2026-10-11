from typing import Generic, TypeVar

_T = TypeVar("_T")

class Registry(Generic[_T]):
    def register(self, item: _T, name: str, priority: float) -> None: ...
    def __getitem__(self, key: str | int) -> _T: ...
