"""Table tests for the title/location classifier.

Precision matters more than recall for alerts (a false alarm costs trust), so
the "trap" rows are as important as the positive ones.
"""

import pytest

from fledge.classify import DegreeTag, degree_tag, is_early_career_tech, is_us

EARLY_CAREER_TECH = [
    "Software Engineer Intern",
    "Software Engineering Intern (Summer 2027)",
    "Data Engineer Intern (2027)",
    "Software Engineer, New Grad",
    "New Graduate Software Engineer",
    "University Grad - Backend Engineer",
    "Machine Learning Engineer Intern",
    "ML Research Intern",
    "AI Engineer Intern",
    "Security Engineer Co-op",
    "Site Reliability Engineer Intern",
    "IT Support Intern",
    "Hardware Engineering Intern",
    "Design Verification Engineer Intern",  # hardware role, not a designer
    "PhD Intern, Software Engineer - Graphics (2027)",
    "Data Science Intern",
    "Entry-Level Software Developer",
    "Early Career Software Engineer",
    "Junior Frontend Developer",
    "Firmware Engineer - Recent Graduate",
    "Software Engineering Apprentice",
    "Interns - Infrastructure",
]

NOT_EARLY_CAREER_TECH = [
    "Early Career, Inside Sales Representative (2027)",  # real-world trap
    "Internal Tools Engineer",  # "internal" is not "intern"
    "International Sales Manager",
    "Senior Software Engineer",
    "Staff Data Scientist",
    "Marketing Intern",
    "Product Designer Intern",
    "Product Manager Intern",
    "Product Marketing Intern",
    "Recruiting Coordinator Intern",
    "Sales Engineer Intern",
    "Finance Intern",
    "Legal Intern",
    "Graduate Recruiter",
    "Account Executive, Enterprise",
    "UX Designer, New Grad",
    "People Operations Intern",
]


@pytest.mark.parametrize("title", EARLY_CAREER_TECH)
def test_early_career_tech_titles(title: str) -> None:
    assert is_early_career_tech(title), title


@pytest.mark.parametrize("title", NOT_EARLY_CAREER_TECH)
def test_rejected_titles(title: str) -> None:
    assert not is_early_career_tech(title), title


@pytest.mark.parametrize(
    ("title", "tag"),
    [
        ("PhD Intern, Data Science (2027)", DegreeTag.PHD),
        ("Ph.D. Research Intern", DegreeTag.PHD),
        ("Doctoral Machine Learning Intern", DegreeTag.PHD),
        ("Master's Software Engineering Intern", DegreeTag.MASTERS),
        ("Masters Intern - Security", DegreeTag.MASTERS),
        ("MS/PhD Intern, AI", DegreeTag.PHD),  # open to PhDs -> strictest wins
        ("Software Engineer Intern", None),
        ("MS Office Support Intern", None),  # Microsoft Office, not a degree
    ],
)
def test_degree_tags(title: str, tag: DegreeTag | None) -> None:
    assert degree_tag(title) is tag


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("San Francisco, CA", True),
        ("San Francisco, CA • New York, NY • United States", True),
        ("San Francisco Bay Area", True),
        ("Remote - US", True),
        ("Remote (USA)", True),
        ("New York City", True),
        ("Seattle, Washington", True),
        ("Austin, TX; Toronto, ON", True),  # any US option counts
        ("Toronto, ON", False),
        ("London, United Kingdom", False),
        ("Dublin, Ireland", False),
        ("Tbilisi, Georgia", False),  # the country, not the state
        ("Bengaluru, India", False),
        ("Remote", None),  # unknown, not "no"
        ("", None),
    ],
)
def test_is_us(location: str, expected: bool | None) -> None:
    assert is_us(location) is expected
