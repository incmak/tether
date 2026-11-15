"""A fake clock for one module under test.

Do not patch functions on the shared time module (patch.object(module.time, 'monotonic', ...)): that changes
time for every thread in the test process. Threads that other tests leave running then call the fake, use up a
side_effect list, move a fake clock, or spin on a sleep that returns at once, and the test fails only in the
full suite. own_time() swaps the module's own `time` name instead, so only that module sees the fake.
"""
import contextlib
from types import SimpleNamespace
from unittest import mock


@contextlib.contextmanager
def own_time(module, **fakes):
    """Patch module.time with its current functions (the real ones, or an outer own_time's fakes), except `fakes`
    (name -> callable). Gives the fake when there is one (like patch.object), else the whole namespace."""
    clock = SimpleNamespace(**{**vars(module.time), **fakes})
    with mock.patch.object(module, 'time', clock):
        yield next(iter(fakes.values())) if len(fakes) == 1 else clock
