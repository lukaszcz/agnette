import re

from markdown import Markdown
from markdown.preprocessors import Preprocessor

RE_NESTED_FENCE_START: re.Pattern[str]

class SuperFencesBlockPreprocessor(Preprocessor):
    lang: str | None
    options: dict[str, str]
    def get_hl_settings(self) -> None: ...
    def clear(self) -> None: ...
    def handle_unrecognized(self, m: re.Match[str]) -> bool: ...
    def handle_attrs(self, m: re.Match[str]) -> bool: ...
    def parse_options(self, m: re.Match[str]) -> bool: ...
    def highlight(
        self,
        src: str,
        language: str,
        options: dict[str, str],
        md: Markdown,
        **kwargs: object,
    ) -> str: ...

def highlight_validator(
    language: str,
    inputs: dict[str, str | bool],
    options: dict[str, str],
    attrs: dict[str, str | bool],
    md: Markdown,
) -> bool: ...
