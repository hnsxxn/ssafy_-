"""Run the tests against the actual standalone Player1 release file."""
import importlib.util
from pathlib import Path
import sys
import unittest

here=Path(__file__).resolve().parent
player=next(here.parent.glob('Player1_*.py'))
spec=importlib.util.spec_from_file_location('battle_bot',player)
module=importlib.util.module_from_spec(spec)
sys.modules['battle_bot']=module
spec.loader.exec_module(module)
sys.path.insert(0,str(here))
suite=unittest.defaultTestLoader.discover(str(here),pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
