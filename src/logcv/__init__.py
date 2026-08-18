"""logcv -- element recognition on scanned geophysical well logs.

Modular by design: shared machinery (banded raster access, unit conversion,
preprocessing, shape features, rendering, reporting) is element-agnostic, and each
element is one plug-in detector. See ``docs/CV_TOOL_DESIGN.md``.

    from logcv.io import LogImage
    from logcv.detectors.registry import build

    with LogImage("log.tif") as img:
        found = build("date_stamp").detect(img)
"""
from .detection import Detection, PageResult
from .io import LogImage
from .units import Box

__all__ = ["Detection", "PageResult", "LogImage", "Box"]
__version__ = "1.5.0"
