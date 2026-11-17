"""Decide which postings Fledge cares about, from title and location text alone.

Scope (DECISIONS.md, 2026-11-16): early-career roles in software, data, ML/AI,
security, infra/IT, hardware. Not PM, design, sales.

These are hand-written rules, so they will miss some titles. We favor precision
(few false alerts) over recall: an unrecognized title is excluded, and every
known trap has a row in tests/test_classify.py. When a real-world miss shows
up, add a test row first, then adjust a pattern.
"""

import re
from enum import Enum

_I = re.IGNORECASE


def _words(*alternatives: str, flags: int = _I) -> re.Pattern[str]:
    """Match any alternative as whole words (so 'intern' never matches 'internal')."""
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", flags)


_EARLY_CAREER = _words(
    r"interns?",
    r"internships?",
    r"co-?op",
    r"apprentice(?:ship)?",
    r"new grad(?:uate)?",
    r"university grad(?:uate)?",
    r"recent grad(?:uate)?",
    r"early career",
    r"entry[- ]level",
    r"junior",
)

_TECH = _words(
    r"software",
    r"engineer(?:ing)?",
    r"developer",
    r"swe",
    r"sde",
    r"data",
    r"machine learning",
    r"ml",
    r"ai",
    r"research",
    r"scientist",
    r"security",
    r"cyber ?security",
    r"infrastructure",
    r"devops",
    r"sre",
    r"site reliability",
    r"systems?",
    r"network(?:ing)?",
    r"hardware",
    r"firmware",
    r"embedded",
    r"back-?end",
    r"front-?end",
    r"full[- ]?stack",
    r"mobile",
    r"ios",
    r"android",
    r"cloud",
    r"platform",
    r"robotics",
    r"computer vision",
    r"silicon",
    r"asic",
    r"fpga",
)
# "IT" must be uppercase, or every title containing the word "it" would match.
_IT = re.compile(r"\bIT\b")

# Any of these means the role is outside our scope, even if a tech word appears
# ("Sales Engineer", "Product Marketing Analyst", "UX Designer").
_EXCLUDED = _words(
    r"sales",
    r"account executive",
    r"marketing",
    r"recruit(?:er|ing|ment)?",
    r"talent",
    r"product manag(?:er|ement)",
    r"program manager",
    r"designer",
    r"product design",
    r"ux",
    r"ui/ux",
    r"finance",
    r"accounting",
    r"legal",
    r"people operations",
    r"hr",
    r"customer success",
)


def is_early_career_tech(title: str) -> bool:
    if not _EARLY_CAREER.search(title):
        return False
    if _EXCLUDED.search(title):
        return False
    return bool(_TECH.search(title) or _IT.search(title))


class DegreeTag(Enum):
    PHD = "phd"
    MASTERS = "masters"


_PHD = _words(r"ph\.?d\.?", r"doctoral", r"doctorate")
# "MS" only counts next to PhD or Master's wording ("MS/PhD"); alone it is
# ambiguous ("MS Office"), and PhD wins in that combination anyway.
_MASTERS = _words(r"master'?s", r"m\.s\.")


def degree_tag(title: str) -> DegreeTag | None:
    """Tag roles that need an advanced degree. If PhDs qualify, PhD wins."""
    if _PHD.search(title):
        return DegreeTag.PHD
    if _MASTERS.search(title):
        return DegreeTag.MASTERS
    return None


_US_STATE_CODES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
]
# Georgia is left out on purpose: it is also a country. ", GA" still matches.
_US_STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    "Florida|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|"
    "Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|"
    "Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
    "North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
    "Wisconsin|Wyoming"
)
_US = re.compile(
    r"\b(?:United States|USA|U\.S\.A?\.?|US|" + _US_STATE_NAMES + r")\b"
    r"|,\s*(?:" + "|".join(_US_STATE_CODES) + r")\b"
    r"|\b(?:San Francisco|Bay Area|Silicon Valley|NYC|New York City|Seattle|Boston|"
    r"Austin|Los Angeles|Chicago|Atlanta|Denver)\b"
)


def is_us(location: str) -> bool | None:
    """True if any listed location is in the US, False if none are.

    None means we can't tell (e.g. just "Remote", or blank). Callers decide how
    to treat unknowns rather than this function guessing.
    """
    text = location.strip()
    if not text or re.fullmatch(r"(?i)remote|anywhere|various|multiple locations", text):
        return None
    return bool(_US.search(text))
