"""Launch the newboom ingestion server.

Thin wrapper: the actual entry point (``main``) lives in ``newboom.api`` so the
installed console script (``newboom``) and this file route to the same code.

Run with ``python main.py`` (or ``uv run python main.py``). Host and port come
from the ``HOST`` / ``PORT`` environment variables (defaults ``0.0.0.0:8000``).
"""

from newboom.api import main

if __name__ == "__main__":
    main()
