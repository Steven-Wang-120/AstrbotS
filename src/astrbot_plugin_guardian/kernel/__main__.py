"""`python -m astrbot_plugin_guardian.kernel` 入口（B16 §9 M3）。"""

from __future__ import annotations

from .service import main

if __name__ == "__main__":
    raise SystemExit(main())
