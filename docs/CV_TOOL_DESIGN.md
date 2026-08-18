# `logcv` — design of the log-element recognition toolkit

Purpose: find and describe **elements** on scanned geophysical well logs
(`data/raw/Digitization_TIF_FINAL/*.TIF`). The first element is the circular
records date stamp ("OCT 1995 / LOG REC'D / RECORDS"); more elements follow
(header block, API number, operator/company, depth scale, curve tracks).

The point of this document is the **extension contract** — how element #2..N get
added without touching element #1 — and the **measurements** the current detector
is calibrated against.

---

## 1. What the imagery is (measured 2026-08-14)

70 TIFFs, one page each, in `data/raw/Digitization_TIF_FINAL/`.

| Property | Value | Consequence for the tool |
|---|---|---|
| Bit depth / mode | 1-bit bilevel (`mode="1"`), one file palette | No grayscale to correlate on; everything is morphology + shape |
| Compression | CCITT Group 4 (69 files), LZW (1) | `MINISWHITE=YES` on the G4 files → raw value 1 = **ink**. Polarity must be normalised on read |
| Resolution | 400 dpi ×57, 200 ×9, 300 ×2, 100 ×2 | **Every threshold must be expressed in inches**, converted per file |
| Page width | 950 – 5200 px (6.6″ – 13.0″) | Header layout is not fixed-width |
| Page height | 10,126 – 528,110 px | A single page is up to 2.75 GP → **never decode a whole page**; PIL would want ~2.7 GB |
| Layout | Strips of 1024 rows, full width | GDAL windowed reads are ~0.02 s; a full-page 1/8-scale decode of the largest file is **4.9 s** |
| Filenames | `<API14>_<imageid>.TIF`; one lacks the `HOU-WL-IMG-1` infix | Filename parsing must be tolerant; API-14 is the join key |
| Junk | `Thumbs.db` in the folder | Filter by extension + `gdal.Open` success, don't crash the batch |

**Feasibility conclusion:** a full-corpus scan costs about ten minutes of CPU.
There is no need for tiling heroics or a GPU; the cost driver is correctness.

## 2. The target element (stamp v1)

Measured from `42089319020000_HOU-WL-IMG-1-1341250.TIF` at 400 dpi, centre
(1259, 1593), **outer radius 0.70 in — 1.40 in across**. The radial ink profile
(mean coverage around the circle, as a fraction of the outer radius) is the
detector's whole basis:

| r / R | ink | what it is |
|---|---|---|
| 0.25 – 0.46 | 0.23 – 0.42 | three lines of inner text (`OCT 1995` / `LOG REC'D` / `RECORDS`) |
| 0.60 – 0.73 | 0.07 – 0.14 | mostly blank gap — **the profile's minimum** |
| 0.80 – 0.94 | 0.22 – 0.47 | annulus of numerals 1…31 (the day dial) + a solid arrowhead |
| **1.01** | **0.63** | the solid outer circle — the profile's peak |
| 1.08 – 1.15 | 0.03 – 0.07 | blank paper outside the stamp |

Rotation is arbitrary (~270° on the reference, because log headers are printed
sideways) and the stamp is pressed **on top of** existing print, so its ring is
crossed by page-length rules and its ink is patchy.

Treated as class **`date_stamp`** — a *circular records date stamp*. The month,
year and day are **attributes**, never part of the detection criterion. If a
1987 or 1993 stamp exists in the corpus, v1 must still find it.

## 3. Architecture

```
src/logcv/
  io.py            LogImage: lazy windowed GDAL reader; dpi + polarity normalisation;
                   .read(box, factor) -> bool ink mask; .bands() overlapping generator
  units.py         Box in page px; inches <-> px per image
  preprocess.py    long-line suppression, despeckle, boundary, gradients
  features/
    circles.py     gradient-voting Hough + annulus matched filter -> candidates
    polar.py       polar resample, radial profile, circular NCC, circle refinement
  detectors/
    base.py        Detector ABC
    registry.py    @register("date_stamp") + build() by name
    date_stamp.py  element #1
  templates/
    date_stamp_v1.npz   reference digit-ring signature + radial profile
  detection.py     Detection / PageResult -- the stable output schema
  render.py        crops, context views, contact sheets  (PIL, never matplotlib)
  report.py        the Excel review workbook + flat CSV
  cli.py           logcv detect | logcv make-template
tests/             pytest suite, weighted to the silent-failure modes
```

### The extension contract

Every element is a `Detector`. Adding element #2 = adding one module under
`detectors/` and registering it. Nothing else changes.

```python
@register("header_block")
class HeaderBlockDetector(Detector):
    version  = "1.0"
    work_dpi = 100.0

    @classmethod
    def defaults(cls) -> dict:      # tunables; lengths in INCHES
        return {"min_width_in": 4.0}

    def detect(self, img: LogImage) -> list[Detection]: ...
```

Two rules keep detectors composable:

1. A detector **takes a `LogImage` and returns `Detection`s**. It never opens or
   writes files and never prints — the CLI and `report.py` own all I/O. So the
   same detector runs in a batch, in a notebook, and under a unit test.
2. A detector always returns a **list**. Zero, one, or several of an element may
   be present, and no caller should assume which.

Shared machinery is element-agnostic on purpose: a header-block detector reuses
the banded reader and the line suppression; an API-number detector reuses the OCR
wrapper and `render`. `--detectors a,b,c` runs several over one pass of the pages.

### Output schema (`Detection`)

Stable across detector versions, so results stay joinable:

```
element_type, detector_name, detector_version,
center_x, center_y, x0, y0, x1, y1        (native page px)
center_in_x, center_in_y, radius_in       (inches - the resolution-free view)
angle_deg, score, decision                (hit | uncertain | miss)
attrs{}                                   (element-specific: month, year, day, upright_ccw_deg, clipped)
evidence{}                                (named sub-scores + the raw measurements behind them)
crop_path, context_path
```

`decision` uses **two** thresholds: above `t_hit` accept, below `t_miss` reject,
between them **`uncertain`** → surfaced for review as `MAYBE`. A corpus this
heterogeneous will not tolerate a single threshold: one setting either leaks false
positives or silently drops faint stamps.

`evidence` carries every raw measurement, so any decision can be explained — or
re-thresholded — without re-running the detector.

## 4. `date_stamp` algorithm (v1)

**Stage 1 — candidates (coarse, whole page, ~100 dpi).** Stream the page in
overlapping bands (overlap > one stamp diameter). Suppress ink runs longer than
1.5 in, horizontal and vertical: the ruled grid vanishes, the ring cannot be
touched (a 0.70-in-radius circle's chord stays `c²/8R` ≈ 20 px from straight over
1.5 in, far outside the 3-px skew tolerance). Then a gradient-voting Hough over
radii 0.55–0.85 in: each boundary pixel votes for a centre at distance R along its
gradient normal, both directions. Cost is O(edge pixels × radii), not
O(pixels × radii), which is what makes a 2.75-GP page affordable, and votes
accumulate happily from a **broken** arc.

**Stage 2 — verify (per candidate, native dpi).** Refine centre and radius by
maximising ink along a single circle, then polar-resample and score the bands in
§2. Rotation invariance comes from the resampling itself, which is the reason to
prefer this over rotated template matching: one pass instead of one per angle, and
the rotation angle falls out of the digit-ring correlation for free.

Sub-scores, with the values that set them (reference stamp vs. the false positives
the coarse pass actually produces — dense tables of figures):

| Sub-score | Measures | Reference | False positives | Weight |
|---|---|---|---|---|
| `angular` | circular NCC of the 1…31 dial vs. template | **1.00** | 0.13 – 0.23 | 0.25 |
| `contrast` | ink on the ring − ink just outside | **≈0.58** | ≈0.07 | 0.20 |
| `outside` | outside ÷ dial density | **0.14** | 0.5 – 1.5 | 0.16 |
| `ring` | fraction of angles where the ring is present | 0.84 | 0.20 – 0.67 | 0.14 |
| `gap` | gap ÷ dial density | 0.34 | 0.6 – 1.5 | 0.10 |
| `digits` | dial band ink density | 0.32 | 0.07 – 0.32 | 0.08 |
| `text` | inner text band ink density | 0.26 | 0.05 – 0.28 | 0.07 |

Each is a soft trapezoid, not a hard threshold — brittleness on heterogeneous
scans comes from hard cut-offs on single measurements. The two heaviest weights go
to the cues a dense table **cannot** fake: a sharp-edged ring with clear paper
outside it, and the angular signature of the dial. Density-in-a-band tests alone
scored a table of figures at 0.88 (see §7).

**Stage 3 — attributes (best-effort, never blocks a detection).** `upright_ccw_deg`
comes from the dial correlation and is what stands the crop up for a human. Month,
year and day (OCR of the inner text; the arrowhead's angle on the dial) are wired
into the schema but **not implemented in v1** — the review workbook shows an
upright crop instead, which for 70 pages is more accurate than OCR and needs no
validation set.

**Stage 4 — report.** See §5.

## 5. The deliverable

`logcv detect` writes an Excel workbook whose first four columns are the contract:

| Col | Contents |
|---|---|
| **A** | log filename |
| **B** | `YES` / `MAYBE` / `NO`, colour-coded |
| **C** | hyperlink that opens the page **at the stamp** |
| **D** | the stamp itself, cropped and stood upright, embedded in the cell |

Then: a review dropdown (OK / CHECK / FAIL), score, ring completeness, position and
diameter in inches, rotation, API-14, dpi, page size, other-candidate count, and
warnings. Plus a `Summary` sheet, `stamp_inventory.csv` (one row per detection, for
joining on API-14), per-page JSON, and contact sheets per verdict.

Column **C** links to a *rendered neighbourhood*, not the TIFF: a hyperlink cannot
position a viewer at a pixel, and these pages run up to 110 ft long, so the tool
renders the 6-inch surround with the stamp ringed in red. Rows with no detection
still get a page thumbnail in **D**, so a negative is something a reviewer can
confirm rather than take on faith.

## 6. Validation

Machine metrics mean nothing without a hand-made truth set, and 70 pages is small
enough to label properly. The workbook **is** the labelling instrument: its
`Reviewed` column turns a batch run into ground truth, after which `t_hit` /
`t_miss` can be set from data instead of from one example.

The honest limitation today: the positive side is calibrated on **one** confirmed
stamp, and the template was cut from that same stamp, so its 1.00 `angular` score
is partly circular. False positives are characterised from real pages and are well
separated (≤0.23). Rotation invariance was verified independently by rotating the
reference through known angles — NCC stayed ≥0.92 at every angle.

## 7. Lessons worth keeping (all three failed *silently*)

1. **Vote aggregation must sum, not average.** A boundary pixel's normal carries a
   few degrees of error, so votes for one circle arrive as a cloud ~0.2 R across
   rather than stacked on one cell. Gaussian-smoothing the accumulator normalises
   by kernel area, so comparing its peak against a `2πR` vote count under-reads by
   ~70×: the detector found *nothing at all* while looking perfectly healthy. Use a
   box **sum**. Guarded by `test_hough_finds_synthetic_ring`.
2. **Refinement must search centre and radius jointly, and about a fixed origin.**
   Sweeping the centre first at a wrong radius locks in a wrong centre; mutating
   the best-so-far inside the sweep lets the origin random-walk (it drifted 17 px).
   And the objective must be a *single* circle preferring the **outermost** strong
   ring — a radial window's maximum is happily satisfied by the dense dial inside
   the outline, which inflated the radius to 0.76 in and, because every band is a
   fraction of R, dropped the true stamp's score from 0.997 to 0.567.
3. **Convert once, outside the loop.** `polar_sample` re-ran `.astype(float32)` on
   the whole crop for each of hundreds of refinement probes — 85 % of total runtime,
   2 GB of pointless conversion per candidate. 37 s/page → 5 s/page.
4. **Don't ask a fitting objective for accuracy it hasn't got — optimise the thing
   you actually care about.** The dial correlation needs the centre to ~2 px: a 4 px
   error (1.4 % of R) takes it from 1.00 to 0.63, 8 px to 0.32, while an **8.6 %
   radius error still scores 0.82**. No circle-fitting objective localises a
   grid-overprinted broken ring that well — substituting ink-on-clean, ring-minus-
   outside contrast, or any tie-break margin moved the optimum by under a pixel.
   The fix is to search a small neighbourhood for the geometry that maximises the
   *match* (`best_signature_match`) and measure every band there. On the first
   corpus run this defect was invisible in the verdicts (precision was 10/10) and
   only showed up in the evidence columns: `raw_angular` was 0.14–0.38 on all 70
   pages *including the page the template was cut from*, and four confirmed stamps
   reported a radius of exactly the configured search maximum. **Check that your
   most discriminative feature is actually discriminating**, per class, not just
   that the answers look right.
5. **A near-periodic signal cannot fix its own phase.** The dial is 31 near-identical
   glyphs 11.6° apart, so correlating it gives 31 competing peaks; using it for
   rotation put 8 of 10 crops at the wrong angle and one upside down. Orientation
   comes from the inner text block instead (unambiguous mod 180°, dial breaks the
   tie), snapped to 90° because the raw estimate is only good to ~15° — the
   partially-suppressed ruled grid pulls it toward the cardinals. Note the synthetic
   fixture for this must be *asymmetric*: a pure `cos(31θ)` ring has 31 equally good
   alignments, so any shift a test asserts on it is arbitrary.
6. **A negative needs evidence too.** The no-detection thumbnail originally showed
   the top 16 in of the page at ~10 dpi, where a 1.4 in stamp is 15 px — and stamps
   are not only in the header: three confirmed ones sit within 3 in of the *bottom*
   of pages 32–64 ft long, on pages that also carry one at the top. The detector
   scanned whole pages throughout, so this was a reporting defect that would have
   made 30 unverifiable "NO" rows look checked.

## 8. Known risks

| Risk | Handling |
|---|---|
| Ring broken by overprinted grid lines | Line suppression before ring search; Hough tolerates arcs; `ring` is a completeness fraction, not a yes/no |
| Stamp clipped at the page edge | Accept partial circles, flag `clipped`, surface it in the workbook |
| Page margin / band cut voting for phantom circles | `boundary()` uses `border_value=1`, so the array edge is not an ink edge |
| 100 dpi pages: stamp only ~140 px across | Working dpi = `min(native, 100)`; verification runs at native, so there is no detail in reserve — expect weaker `angular` |
| Other stamp designs / dates exist (unconfirmed) | Class is "circular date stamp"; date is an attribute. The `MAYBE` band and the no-detection thumbnails are where a second design would show up |
| More than one stamp per page | Detector returns a list; `max_per_page` caps it at 6 |
| Positive side calibrated on one example | Review the workbook, then re-derive thresholds — see §6 |
| conda-forge matplotlib vs. GDAL freetype DLL clash on this workstation | All rendering is PIL. If matplotlib is ever needed here, import it *before* GDAL |
