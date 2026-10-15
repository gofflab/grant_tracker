"""Build a draft Opportunity from an RFA link, announcement number, uploaded file or pasted text."""

from .pipeline import Result, looks_like_number, run

__all__ = ["Result", "looks_like_number", "run"]
