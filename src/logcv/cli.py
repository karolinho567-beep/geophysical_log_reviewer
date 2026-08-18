"""Command line for logcv.

    python -m logcv detect --in <folder> --out <dir> [--detectors date_stamp]
    python -m logcv make-template --log <tif> --out <npz> [--upright-ccw 90]
    python -m logcv review [--in <folder>] [--xlsx <workbook>]

The CLI owns every side effect -- reading folders, rendering images, writing the
workbook -- so detectors stay pure functions of a page and remain testable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import render, report
from .detection import PageResult
from .detectors.registry import available, build_many
from .io import LogImage, iter_logs


def _detect(args: argparse.Namespace) -> int:
    paths = iter_logs(args.input, args.pattern)
    if not paths:
        print(f"no rasters matching {args.pattern!r} in {args.input}", file=sys.stderr)
        return 2
    if args.limit:
        paths = paths[: args.limit]

    detectors = build_many(args.detectors.split(","))
    print(f"{len(paths)} logs, detectors: {', '.join(d.name for d in detectors)}")

    crops = os.path.join(args.out, "qc", "crops")
    contexts = os.path.join(args.out, "qc", "context")
    detections_dir = os.path.join(args.out, "detections")
    for folder in (crops, contexts, detections_dir):
        os.makedirs(folder, exist_ok=True)

    pages: list[PageResult] = []
    assets: dict[str, report.RowAssets] = {}

    for i, path in enumerate(paths, start=1):
        started = time.time()
        with LogImage(path) as img:
            found = []
            for detector in detectors:
                found.extend(detector.detect(img))
            page = PageResult(
                source_file=path, api14=img.api14, width=img.width, height=img.height,
                dpi=img.dpi, dpi_source=img.dpi_source, detections=found,
                warnings=list(img.warnings), seconds=time.time() - started,
            )
            asset = _render_row(img, page, crops, contexts, args.out)

        pages.append(page)
        assets[path] = asset
        stem = os.path.splitext(os.path.basename(path))[0]
        with open(os.path.join(detections_dir, f"{stem}.json"), "w", encoding="utf-8") as handle:
            handle.write(page.to_json())

        best = page.best
        print(f"  [{i:>3}/{len(paths)}] {os.path.basename(path):46s} "
              f"{report.verdict_for(page):5s} "
              f"{'score %.3f' % best.score if best else '          '} "
              f"{page.seconds:5.1f}s")

    xlsx = report.write_workbook(os.path.join(args.out, args.workbook), pages, assets)
    csv_path = report.write_csv(os.path.join(args.out, "stamp_inventory.csv"), pages)
    _write_sheets(pages, assets, args.out)

    counts = {v: sum(1 for p in pages if report.verdict_for(p) == v)
              for v in ("YES", "MAYBE", "NO")}
    print(f"\n{counts['YES']} with a stamp, {counts['MAYBE']} to check, {counts['NO']} without")
    print("workbook:", xlsx)
    print("csv:     ", csv_path)
    return 0


def _render_row(img: LogImage, page: PageResult, crops: str, contexts: str,
                out_root: str) -> report.RowAssets:
    """Render the crop and the context view backing one workbook row."""
    stem = os.path.splitext(os.path.basename(page.source_file))[0]
    best = page.best
    asset = report.RowAssets()

    if best is not None:
        crop = render.crop_image(img, best, rotate_ccw_deg=best.attrs.get("upright_ccw_deg"))
        crop_path = os.path.join(crops, f"{stem}_stamp.png")
        render.save(crop, crop_path)
        best.crop_path = crop_path
        asset.crop_rel = os.path.relpath(crop_path, out_root)

        context = render.context_image(img, best)
        context_path = os.path.join(contexts, f"{stem}_context.jpg")
        render.save(context, context_path)
        best.context_path = context_path
        asset.context_rel = os.path.relpath(context_path, out_root)
    else:
        # No detection: show BOTH ENDS of the page, because stamps are not only in
        # the header -- confirmed cases sit within 3 in of the bottom of 32-64 ft
        # pages. A header-only thumbnail cannot support a negative.
        thumb = render.page_ends(img)
        crop_path = os.path.join(crops, f"{stem}_ends.png")
        render.save(thumb, crop_path)
        asset.crop_rel = os.path.relpath(crop_path, out_root)

        full = render.page_ends(img, top_in=20.0, bottom_in=16.0,
                                out_dpi=110.0, max_px=2600)
        context_path = os.path.join(contexts, f"{stem}_ends.jpg")
        render.save(full, context_path)
        asset.context_rel = os.path.relpath(context_path, out_root)

    return asset


def _write_sheets(pages: list[PageResult], assets: dict[str, report.RowAssets],
                  out_root: str) -> None:
    """Contact sheets per verdict, for reviewing the whole batch in three glances."""
    from PIL import Image

    groups: dict[str, list[tuple[Image.Image, str]]] = {"YES": [], "MAYBE": [], "NO": []}
    for page in pages:
        asset = assets.get(page.source_file)
        if not asset or not asset.crop_rel:
            continue
        path = os.path.join(out_root, asset.crop_rel)
        if not os.path.exists(path):
            continue
        best = page.best
        label = os.path.basename(page.source_file)[:14]
        if best is not None:
            label += f" {best.score:.2f}"
        groups[report.verdict_for(page)].append((Image.open(path), label))

    for verdict, items in groups.items():
        if not items:
            continue
        sheet = render.contact_sheet([i for i, _ in items], [l for _, l in items])
        render.save(sheet, os.path.join(out_root, "qc", f"sheet_{verdict.lower()}.png"))


def _make_template(args: argparse.Namespace) -> int:
    """Build the reference dial signature from one or more known-good stamps.

    Averaging several confirmed stamps matters for two reasons: a template cut from a
    single stamp scores 1.00 against itself, which tells you nothing about how it
    generalises, and the *die* is the thing being modelled, not one impression of it.
    The day arrow and the date text differ between impressions, so averaging aligned
    signatures reinforces the 1..31 dial they share and suppresses what they don't.
    """
    import numpy as np

    from .detectors.date_stamp import BAND_DIGITS, _POLAR_R_HI, _POLAR_R_LO, DateStampDetector
    from .features import polar as P
    from .preprocess import suppress_long_lines
    from .units import Box

    detector = DateStampDetector(template_npz=False)
    collected: list[tuple[str, np.ndarray, np.ndarray, float]] = []

    for path in args.log:
        with LogImage(path) as img:
            found = detector.detect(img)
            best = max(found, key=lambda d: d.score) if found else None
            if best is None or best.decision == "miss":
                print(f"  {os.path.basename(path):46s} no stamp - skipped", file=sys.stderr)
                continue

            radius = (best.x1 - best.x0) / 2.0
            box = Box.centered(best.center_x, best.center_y,
                               int(radius * 2)).clip(img.width, img.height)
            clean = suppress_long_lines(img.read(box), run_px=img.px(0.35))
            cx, cy = best.center_x - box.x0, best.center_y - box.y0

            if collected:  # let the match settle the geometry against what we have
                mean = _mean_signature([s for _, s, _, _ in collected])
                match = P.best_signature_match(clean, cx, cy, radius, mean, BAND_DIGITS,
                                               search_px=max(4.0, radius * 0.06),
                                               step_px=max(1.0, radius * 0.01))
                cx, cy, radius = match.cx, match.cy, match.radius
                shift = match.shift_bins
            else:
                shift = 0

            polar = P.polar_sample(clean, cx, cy, radius, r_lo=_POLAR_R_LO, r_hi=_POLAR_R_HI,
                                   n_r=56, n_theta=360)
            signature = P.angular_signature(polar, *BAND_DIGITS, _POLAR_R_LO, _POLAR_R_HI)
            # roll into the first stamp's rotational frame before averaging
            aligned = np.roll(signature, -shift)
            collected.append((os.path.basename(path), aligned,
                              P.radial_profile(polar), best.radius_in))
            print(f"  {os.path.basename(path):46s} score {best.score:.3f} "
                  f"r={best.radius_in:.2f}in shift={shift:3d}")

    if not collected:
        print("no usable stamps found", file=sys.stderr)
        return 2

    signature = _mean_signature([s for _, s, _, _ in collected])
    profile = np.mean([p for _, _, p, _ in collected], axis=0)
    radii = [r for _, _, _, r in collected]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    np.savez(
        args.out,
        digit_signature=signature,
        radial_profile=profile,
        radial_grid=np.linspace(_POLAR_R_LO, _POLAR_R_HI, 56),
        radius_in=float(np.median(radii)),
        n_sources=len(collected),
        # Rotation that stands the first source's die upright; measured by eye once.
        upright_ccw_deg=float(args.upright_ccw),
    )
    print(f"wrote {args.out} from {len(collected)} stamps "
          f"(median radius {np.median(radii):.2f} in)")
    return 0


def _mean_signature(signatures: list) -> "object":
    """Mean of z-scored signatures, so heavily-inked stamps do not dominate."""
    import numpy as np

    stack = []
    for sig in signatures:
        centred = np.asarray(sig, dtype=np.float64) - np.mean(sig)
        norm = np.linalg.norm(centred)
        if norm > 1e-9:
            stack.append(centred / norm)
    return np.mean(stack, axis=0)


def _review(args: argparse.Namespace) -> int:
    """Delegate to the standalone review product's canonical entry point."""
    from .review.cli import execute

    return execute(
        folder=args.input,
        workbook=args.xlsx,
        cache_dir=getattr(args, "cache", None),
        selftest_mode=getattr(args, "selftest", False),
        report=getattr(args, "report", None),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logcv", description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="command", required=True)

    d = subs.add_parser("detect", help="find elements on every log in a folder")
    d.add_argument("--in", dest="input", required=True, help="folder of scanned logs")
    d.add_argument("--out", required=True, help="output folder (a task's outputs/)")
    d.add_argument("--detectors", default="date_stamp",
                   help=f"comma-separated; available: {','.join(available())}")
    d.add_argument("--pattern", default="*.tif", help="filename glob, case-insensitive")
    d.add_argument("--workbook", default="stamp_review.xlsx")
    d.add_argument("--limit", type=int, default=0, help="stop after N logs (for a trial run)")
    d.set_defaults(func=_detect)

    t = subs.add_parser("make-template", help="build the reference signature asset")
    t.add_argument("--log", required=True, nargs="+",
                   help="one or more logs carrying confirmed stamps (averaged)")
    t.add_argument("--out", required=True, help="output .npz")
    t.add_argument("--upright-ccw", type=float, default=0.0,
                   help="degrees CCW that stand the reference stamp upright")
    t.set_defaults(func=_make_template)

    r = subs.add_parser("review", help="hand-review the logs in a window (stamp / no stamp)")
    r.add_argument("--in", dest="input", default=None,
                   help="folder of scanned logs (a folder chooser opens if omitted)")
    r.add_argument("--xlsx", default=None,
                   help="review workbook (default: ./reviews/"
                        "<folder>_stamp_review.xlsx); resumed if it already exists")
    r.add_argument("--cache", default=None,
                   help="pyramid cache directory (default: ./cache beside the app)")
    r.add_argument("--selftest", action="store_true",
                   help="drive the app once and exit; 0 = healthy (verifies a build)")
    r.add_argument("--report", default=None,
                   help="with --selftest, write the check list to this file")
    r.set_defaults(func=_review)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
