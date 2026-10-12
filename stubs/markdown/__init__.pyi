from collections.abc import Mapping, Sequence

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from markdown.util import Registry

class Markdown:
    preprocessors: Registry[Preprocessor]
    def __init__(
        self,
        *,
        extensions: Sequence[str | Extension] = ...,
        extension_configs: Mapping[str, Mapping[str, object]] = ...,
    ) -> None: ...
    def convert(self, source: str) -> str: ...
    def reset(self) -> Markdown: ...
