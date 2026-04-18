"""asw – The Agentic Software Organization."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("asw")
except PackageNotFoundError:
    __version__ = "0+unknown"
