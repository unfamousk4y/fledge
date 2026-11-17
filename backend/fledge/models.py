"""Source-independent data shapes shared by crawlers, the detector, and (later) the DB."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Posting:
    """One job posting as it appears on a company's hiring feed right now."""

    source: str  # which hiring system, e.g. "greenhouse"
    board: str  # the company's board token on that system, e.g. "figma"
    external_id: str  # the posting's ID on that system; unique within a board
    title: str
    location: str  # free text as the company wrote it; "" if missing
    url: str
    first_published: datetime | None  # the company's own publish time, if provided
    updated_at: datetime | None
