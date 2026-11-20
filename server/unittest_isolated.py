"""Import first in every test module: moves HOME, TMPDIR and the stores to a fresh folder before the module builds fixtures."""
from unittest_environment import isolate

isolate()
