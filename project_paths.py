"""Where the project lives, whichever layout it is run from.

In the repository the code sits in folders (pipeline/, quant/, ...) while data/
stays at the top. On the server every file is copied into one flat folder next
to data/. This module always sits at that top level, so its own location is
the answer in both layouts. Set OPTIONS_ENGINE_ROOT to override.
"""
import os

ROOT = os.environ.get("OPTIONS_ENGINE_ROOT") or os.path.dirname(os.path.abspath(__file__))
