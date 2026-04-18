from .registry import Rule, Finding, registry, evaluate, register

# Side-effect imports register each rule into `registry`.
# Order matters: more specific rules first. The CLI's per-field dedup keeps
# the first finding that writes each XMP field, so sibling-file-specific
# rules (dji-*) beat folder-wide fallbacks (trip-gps-anchor).
from . import dji_srt  # noqa: F401
from . import filename_date  # noqa: F401
from . import insta360  # noqa: F401
from . import trip_gps  # noqa: F401
from . import trip_tags  # noqa: F401
from . import trip_timezone_guess  # noqa: F401
from . import trip_timezone  # noqa: F401
from . import clock_drift  # noqa: F401
from . import clock_drift_by_camera  # noqa: F401
from . import tag_suggest  # noqa: F401
from . import export_date_trap  # noqa: F401
from . import bloat_candidate  # noqa: F401

__all__ = ["Rule", "Finding", "registry", "evaluate", "register"]
