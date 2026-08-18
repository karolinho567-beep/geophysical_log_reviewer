# Project structure & conventions

A reusable layout for data + GIS + code analysis projects.

## Three lanes + tasks

- **data/** — datasets, in three stages of doneness
- **gis/** — QGIS and ArcGIS projects and shared layers
- **src/** — shared reusable code
- **packaging/** — platform-specific executable builders for reusable products
- **build/** / **dist/** — disposable build scratch / generated distributions
- **tasks/** — one folder per analysis question, pointing at the lanes above
- **deliverables/** — the single outward-facing output location

## The data stages

| Bucket | What | Regenerable? | Test |
|---|---|---|---|
| `data/raw/` | as-received, immutable | n/a (never touch) | "Did it arrive this way?" |
| `data/interim/` | mid-pipeline intermediates | yes, throwaway | "Can I delete this and nothing breaks?" |
| `data/processed/` | trusted analysis-ready layers | yes, but costly | "Do multiple tasks need this?" |
| `tasks/<t>/outputs/` | scratch results for ONE question | yes | "Is this specific to one analysis?" |
| `deliverables/` | final products for people outside | no, hand-finished | "Does someone outside see this?" |

## Where results go — the write rule

**Every task writes to exactly one place: `tasks/<task>/outputs/`.** No result
files at the task root (only `README.md`, scripts, configs live there), no
invented output folders (`qa_outputs/`, `results/`, etc.), no writing into
`data/` or `deliverables/` as a side effect of running something.

Files leave `outputs/` only by **explicit promotion**:

- → `deliverables/<category>/` when a result is **approved** — the final version
  only, under its deliverable name; drafts stay behind in the task. Promote the
  same day it's approved, and log it in the ledger (`deliverables/README.md`)
  and PROJECT_STATUS.md.
- → `data/processed/` only when **another task will read it as input**. It's an
  input folder for downstream work, not a results folder. Test: "will a second
  task load this by path?" If no, it stays in the task's `outputs/`.

So a result is only ever in one of two places:
**`deliverables/` if it's final, the producing task's `outputs/` if it's not** —
and PROJECT_STATUS.md + `deliverables/README.md` index both.

## The three rules

1. **`data/raw/` is read-only.** Nothing writes or edits there. Tasks read from it.
2. **Reference, don't copy.** Tasks point at `data/` by path so the project doesn't bloat or fork.
3. **Promote keepers, and log the promotion.** Scratch lives in `tasks/*/outputs/`;
   approved products move to `deliverables/` (recorded in its ledger); datasets
   consumed by other tasks move to `data/processed/`.

## GIS hygiene

- Set QGIS projects to **relative paths** (Project Properties → General) so moving the project folder doesn't break layers.
- Keep `.gdb` inside `gis/arcgis/`; don't scatter geodatabases across `data/`.

## Reusable software products

Application behavior belongs in the installable package under `src/`. A
`pyproject.toml` at the project root defines package metadata and command-line
entry points. Platform builders live under `packaging/<platform>/` and contain
only build configuration or thin wrappers; they must call the package's canonical
entry point. Generated build scratch and distributions go to `build/` and `dist/`,
not to an analysis task.

## Starting a new project

1. Copy `_TEMPLATE_PROJECT/` and rename it.
2. Update `README.md`, `environment.yml` (env name), and `.env.example`.
3. Rename the QGIS project and fix its title. A `.qgz` is a zip holding a
   `.qgs` XML file that still says `_TEMPLATE_PROJECT`, so renaming the file
   alone leaves the QGIS title bar reading **_TEMPLATE_PROJECT**. Rename
   `gis/qgis/_TEMPLATE_PROJECT.qgz` → `<project>.qgz`, then unzip it, replace
   every `_TEMPLATE_PROJECT` inside the `.qgs` (the `projectname=` attribute,
   the project `<title>`, and the layout `<title>`), rename the inner `.qgs`,
   and re-zip. The project CRS is already **EPSG:10481** (NAD83 / TWDB GM,
   us-ft) — leave it.
4. Drop incoming data into `data/raw/<source>/`.
5. Make a `tasks/<question>/` folder per deliverable.
