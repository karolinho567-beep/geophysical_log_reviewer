# src — shared reusable code

Code used by more than one task lives here as an importable package
(e.g. `src/<projectpkg>/sampling.py`). Tasks import from it instead of copy-pasting.

One-off, question-specific scripts belong in `tasks/<task>/`, not here.

## `logcv` — element recognition on scanned geophysical logs

Finds and describes **elements** on the scanned well logs in
`data/raw/Digitization_TIF_FINAL/`. Element #1 is the circular records date stamp;
the package is built so that elements #2..N (header block, API number, operator,
depth scale, curve tracks) are one new file each.

```powershell
$env:PYTHONPATH="src"
python -m logcv detect --in data\raw\Digitization_TIF_FINAL --out tasks\<task>\outputs
python -m pytest tests -q
```

```python
from logcv.io import LogImage
from logcv.detectors.registry import build

with LogImage(path) as img:          # lazy, windowed: pages reach 2.75 gigapixels
    found = build("date_stamp").detect(img)
```

| Module | Responsibility |
|---|---|
| `io.py` | `LogImage` — windowed GDAL reads, dpi and ink-polarity normalised once |
| `units.py` | `Box`; inches ↔ px, because the corpus mixes 100/200/300/400 dpi |
| `preprocess.py` | long-line (ruled-grid) suppression, despeckle, boundary, gradients |
| `features/circles.py` | gradient-voting Hough ring finder, annulus matched filter |
| `features/polar.py` | polar resample, circular NCC, circle refinement, template match |
| `features/orientation.py` | which way up an element is, from its linear structure |
| `detectors/` | one module per element + the name registry |
| `render.py`, `report.py` | QC images (PIL only) and the Excel review workbook |
| `review/` | the hand-review product: tiled/pyramidal rendering, workbook store, CLI, and Tk UI |
| `cli.py` | `detect`, `make-template` and `review`; owns all I/O |

**Hand review** is the human counterpart of `detect` — a person pages through the
folder and records stamp / no stamp / which kind, and the answers land in an Excel
workbook the automated run can be scored against:

```powershell
$env:PYTHONPATH="src"
python -m logcv review          # also: python -m logcv.review
```

With the project installed editable, the dedicated product command is simpler:

```powershell
pip install -e . --no-deps
log-review
```

Windows executable tooling is isolated under `packaging/windows`; it imports this
same entry point and contains no application behavior.

**Adding an element:** subclass `Detector`, decorate with `@register("name")`,
implement `detect(img) -> list[Detection]`. Detectors never touch the filesystem and
never print — the CLI and `report.py` own I/O, so the same detector works in a batch
run, a notebook, and a unit test. Read
[`docs/CV_TOOL_DESIGN.md`](../docs/CV_TOOL_DESIGN.md) first, especially §7 — every
lesson in it came from a bug that failed *silently*.
