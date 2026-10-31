from pathlib import Path
import sys

# Direct execution lets a scoped Hook use this checkout without installing it.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from akf.cli import main

raise SystemExit(main())
