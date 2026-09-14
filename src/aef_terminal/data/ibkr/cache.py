from __future__ import annotations

from collections import OrderedDict


class _BoundedCache(OrderedDict):
    """Small LRU cache for qualified IBKR contracts.

    Contract objects can be large, and symbol/account/port combinations change
    during diagnostics. Keep active subscription caches separate; this is only
    for reusable contract metadata.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = max(int(maxsize), 1)

    def __setitem__(self, key, value) -> None:
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)

    def get(self, key, default=None):
        if key not in self:
            return default
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value
