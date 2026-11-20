"""Run the shared unittest isolation without adding server/ to the import path."""
import importlib.util
from pathlib import Path
import sys


if 'unittest_environment' not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        'unittest_environment', Path(__file__).resolve().parents[1] / 'server' / 'unittest_environment.py',
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

sys.modules['unittest_environment'].isolate()
