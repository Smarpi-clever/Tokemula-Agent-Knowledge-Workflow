"""Use the shared installed CLI so a package upgrade also reaches plugin hooks."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from akf.install import Manager

registry = Manager().registry()
if registry.get("installed"):
    os.execv(registry["launcher"], [registry["launcher"], "managed-hook", "--mode", "plugin"])
else:
    print("{}")
