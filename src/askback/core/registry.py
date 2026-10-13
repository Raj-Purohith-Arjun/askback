"""Name -> implementation lookup, so config files can pick components by string.

Why this exists: a future dev swaps a component by editing one line of YAML
instead of editing code. This module is the mechanism plus the one table the
framework owns; a package with its own extension points makes its own tables
from `Registry` (model providers in `askback.providers.base`, the simulator's
agents, retrievers and tools in `askback_sim.registry`).

    from askback.core.registry import EVALUATORS

    @EVALUATORS.register("tone")
    class ToneEvaluator(Evaluator):
        ...

    # your config file -> evaluation.evaluators: [tone]
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Generic, TypeVar

from .errors import RegistryError

T = TypeVar("T")


class Registry(Generic[T]):
    """A typed, self-documenting plugin table."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, obj: T | None = None) -> Callable[[T], T] | T:
        """Register `obj` under `name`. Usable as a decorator or a direct call."""
        key = name.strip().lower()

        def _add(target: T) -> T:
            if key in self._items:
                raise RegistryError(
                    f"{self.kind} '{key}' is already registered by "
                    f"{self._items[key]!r}. Pick a different name."
                )
            self._items[key] = target
            return target

        return _add if obj is None else _add(obj)

    def get(self, name: str) -> T:
        key = name.strip().lower()
        try:
            return self._items[key]
        except KeyError:
            raise RegistryError(
                f"Unknown {self.kind} '{name}'. Registered: {sorted(self._items) or '<none>'}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._items)

    def __contains__(self, name: str) -> bool:
        return name.strip().lower() in self._items

    def __iter__(self) -> Iterator[tuple[str, T]]:
        return iter(sorted(self._items.items()))

    def __len__(self) -> int:
        return len(self._items)


# The framework's own extension point. Import and decorate to extend.
EVALUATORS: Registry[Any] = Registry("evaluator")
