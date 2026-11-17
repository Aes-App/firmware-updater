from __future__ import annotations

import queue
from typing import Callable


def drain(q: "queue.Queue", handle: Callable[[str, object], None],
          on_error: Callable[[str, BaseException], None]) -> None:
    while True:
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            return
        except Exception:
            continue
        try:
            handle(kind, payload)
        except Exception as e:
            try:
                on_error(kind, e)
            except Exception:
                pass
