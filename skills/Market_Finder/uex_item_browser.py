# Market Finder — powered by uexcorp.space API v2
#
# This is the sole entry point.  The sys.path adjustment here is the
# only place it exists — all internal modules use relative imports.

import os
import sys

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from market_finder.ui.app import main  # noqa: E402


if __name__ == "__main__":
    main()
