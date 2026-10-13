from __future__ import annotations

from threading import RLock
from weakref import WeakSet


class OperationCancelled(Exception):
    """Raised when a runtime operation is cancelled."""

    def __init__(self, reason: str = "Operation cancelled.") -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """Cancel an operation and every child operation created from it."""

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._children: WeakSet[CancellationToken] = WeakSet()
        self._lock = RLock()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "Operation cancelled.") -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason
            children = list(self._children)
        for child in children:
            child.cancel(reason)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled(self.reason or "Operation cancelled.")

    def child(self) -> CancellationToken:
        child = CancellationToken()
        with self._lock:
            if self._cancelled:
                child.cancel(self._reason or "Operation cancelled.")
            else:
                self._children.add(child)
        return child
