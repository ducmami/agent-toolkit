"""Allow ``python -m doconvert`` when ``scripts/`` is on ``sys.path``."""

from .doconvert import main

raise SystemExit(main())
