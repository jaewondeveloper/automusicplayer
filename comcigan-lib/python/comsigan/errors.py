class ComciganError(Exception):
    """Base error for comcigan client."""


class ParseError(ComciganError):
    """Failed to parse /st bootstrap page (routes may have changed)."""


class SchoolNotFoundError(ComciganError):
    """School search returned no results."""


class TimetableError(ComciganError):
    """Timetable request failed or grade/class is out of range."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code
