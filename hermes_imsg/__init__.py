"""iMessage for Hermes Agent through `imsg`.

`register(ctx)` is the Hermes plugin entry point, both for a pip install (the
`hermes_agent.plugins` entry point) and for this directory dropped into a Hermes
plugins folder (with `plugin.yaml`). Everything else here imports without Hermes.
"""

from typing import Any


def register(ctx: Any) -> None:
    from .platform import register as _register  # imports Hermes; only Hermes calls this

    _register(ctx)
