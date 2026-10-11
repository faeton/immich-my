"""teslacam — Tesla dashcam dumps → reviewed, trimmed, stack-ready render folders.

Pipeline: `scan` (index + telemetry) → `review` (local viewer writes
decisions) → `render` (mosaic proxy + per-camera lossless concats + manifest).
`immy promote` ships the render folder; `immy stacks apply` makes the stacks.
Raw dumps are never modified. See docs/TESLA-STACKS-DESIGN.md.
"""

__all__ = ["__version__"]
__version__ = "0.0.1"
