import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.path[1:2] and Path(sys.path[1]).resolve() == Path(__file__).resolve().parent:
    del sys.path[1]
from pdbchain.cli import main  # noqa: E402

sys.exit(main())
