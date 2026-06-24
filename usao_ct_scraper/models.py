from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class PressRelease:
    title: str
    url: str
    date: Optional[str]
    date_published: Optional[str]
    body_text: str
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self):
        return asdict(self)
