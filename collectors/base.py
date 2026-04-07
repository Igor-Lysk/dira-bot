"""Base collector interface (inspired by flathunter's modular design)."""

from abc import ABC, abstractmethod


class BaseCollector(ABC):
    """Every data source implements this interface."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Unique name: 'telegram', 'yad2', 'facebook', etc."""

    @abstractmethod
    async def collect(self) -> list[dict]:
        """Return list of raw listing dicts with at minimum:
        {
            "source_id": str,       # ID within the source
            "raw_text":  str,       # full text of the listing
            "url":       str|None,  # link to the listing
        }
        """
