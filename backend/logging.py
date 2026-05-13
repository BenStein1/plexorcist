import json
import logging
from pathlib import Path
from typing import Any


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))


class AuditLogger:
    def __init__(self, path: str = "plexorcist.log") -> None:
        self.path = Path(path)
        self.logger = logging.getLogger("plexorcist.audit")

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {"event_type": event_type, **payload}
        self.logger.info("%s", json.dumps(record, default=str))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
