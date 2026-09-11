"""Allow ``python -m r2tool`` when ``scripts/`` is on ``sys.path``."""

from .r2tool import main

raise SystemExit(main())
