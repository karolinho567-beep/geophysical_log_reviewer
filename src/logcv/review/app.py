"""The review window: a log explorer with a two-click verdict and a Save button.

Layout

    ┌ toolbar: folder, workbook, save, progress ───────────────────────────┐
    │ list          │ canvas viewer (drag/scrollbar to pan, wheel zoom)   │
    │ ✓ file stamp  ├──────────────────────────────────────────────────────┤
    │               │ zoom / jump buttons + page facts                     │
    ├───────────────┴──────────────────────────────────────────────────────┤
    │ Stamp draft │ Log types │ Notes              [Add entry →] │
    └──────────────────────────────────────────────────────────────────────┘

Two things keep it usable on 2.75-gigapixel pages:

* every render is a windowed read of just the visible rectangle (`pages`), and
* renders run on a worker thread, so panning a huge page never freezes the UI.

Keyboard (the point of the whole thing -- 70 pages by mouse is a bad afternoon):

    Y / N        has stamp / has no stamp        1..9   pick the nth stamp type
    Space        next unreviewed page            Ctrl+S save workbook
    Ctrl+Right / Ctrl+Left   next / previous     T / B  jump to page top / bottom
    wheel, + / - zoom in / out                   W / F  fit width / fit whole page
    arrows/drag/scrollbar pan                    Esc    back to the viewer
"""
from __future__ import annotations

import datetime as _dt
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import ImageTk

from logcv import __version__

from . import pages
from .store import (
    InvalidWorkbook,
    ReviewStore,
    WorkbookInspection,
    WorkbookLocked,
    join_values,
    load_log_types,
    load_stamp_types,
    save_stamp_types,
    split_values,
    stamp_types_path,
    validate_review_workbook,
)
from .updates import (
    ReleaseInfo,
    StagedUpdate,
    download_and_stage,
    fetch_latest_release,
    launch_staged_update,
    record_update_check,
    update_check_due,
)

#: Zoom ladder, screen px per page px. Fit-to-page can sit below the bottom rung.
ZOOM_LEVELS = [
    1 / 512, 1 / 384, 1 / 256, 1 / 192, 1 / 128, 1 / 96, 1 / 64, 1 / 48,
    1 / 32, 1 / 24, 1 / 16, 1 / 12, 1 / 8, 1 / 6, 1 / 4, 1 / 3, 1 / 2,
    0.75, 1.0, 1.5, 2.0, 3.0, 4.0,
]
#: How many rendered viewports the worker keeps, so revisiting a page is instant.
RENDER_CACHE = 8
#: Re-render this long after the last pan/resize event, to coalesce a drag.
RENDER_DEBOUNCE_MS = 90

CHECK = "✔"      # green tick: reviewed
WARN = "⚠"       # said yes, no type picked yet
DONE_FG = "#1a7f37"
WARN_FG = "#b26a00"
TODO_FG = "#8a8a8a"


def _parse_subset_api_lines(
    raw_lines: list[str],
) -> tuple[list[str], int, list[tuple[int, str]]]:
    """Return ordered unique APIs, duplicate count, and invalid line details."""
    requested: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    invalid: list[tuple[int, str]] = []
    for line_number, raw in enumerate(raw_lines, start=1):
        value = raw.strip()
        if not value:
            continue
        if len(value) != 14 or not value.isdigit():
            invalid.append((line_number, value))
            continue
        if value in seen:
            duplicates += 1
            continue
        seen.add(value)
        requested.append(value)
    return requested, duplicates, invalid


@dataclass(frozen=True)
class _Request:
    token: int
    path: str
    vx: float
    vy: float
    scale: float
    vw: int
    vh: int
    mode: str = "mean"

    @property
    def key(self) -> tuple:
        return (self.path, round(self.vx, 2), round(self.vy, 2),
                round(self.scale, 8), self.vw, self.vh, self.mode)


@dataclass(frozen=True)
class _EntryDraft:
    """Canonical editable state for one log, separate from the saved record."""

    has_stamp: bool | None
    stamp_types: tuple[str, ...]
    log_types: tuple[str, ...]
    notes: str

    @classmethod
    def from_record(cls, record) -> "_EntryDraft":
        return cls(
            record.has_stamp,
            tuple(split_values(record.stamp_type)),
            tuple(split_values(record.log_types)),
            record.notes,
        )


class _RenderWorker(threading.Thread):
    """One thread, one pending request: a stale viewport is never worth drawing."""

    def __init__(self, results: "queue.Queue", cache_root: str):
        super().__init__(daemon=True, name="logcv-review-render")
        self._results = results
        self._lock = threading.Condition()
        self._pending: _Request | None = None
        self._shutdown_requested = False
        self._cache_root = cache_root
        self._open: dict[str, object] = {}
        self._cache: dict[tuple, object] = {}

    def submit(self, request: _Request) -> None:
        with self._lock:
            self._pending = request
            self._lock.notify()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_requested = True
            self._lock.notify()

    def _should_abort_prefetch(self) -> bool:
        with self._lock:
            return self._shutdown_requested or self._pending is not None

    # ----------------------------------------------------------------- thread

    def run(self) -> None:  # pragma: no cover - UI thread
        while True:
            with self._lock:
                while self._pending is None and not self._shutdown_requested:
                    self._lock.wait()
                if self._shutdown_requested:
                    break
                request, self._pending = self._pending, None
            try:
                image = self._render(request)
                self._results.put((request, image, None))
                # The requested viewport reaches the UI first. Only then spend
                # idle worker time warming the rows most likely to be scrolled to.
                if not self._should_abort_prefetch():
                    pages.prefetch_viewport(
                        self._page(request.path),
                        request.vx,
                        request.vy,
                        request.scale,
                        request.vw,
                        request.vh,
                        request.mode,
                        self._should_abort_prefetch,
                    )
            except Exception as exc:  # a bad file must not kill the viewer
                self._results.put((request, None, exc))
        for page in self._open.values():
            page.close()

    def _page(self, path: str):
        page = self._open.get(path)
        if page is None:
            page = pages.open_page(path, cache_root=self._cache_root)
            self._open[path] = page
            while len(self._open) > 2:
                oldest = next(iter(self._open))
                self._open.pop(oldest).close()
        return page

    def _render(self, request: _Request):
        cached = self._cache.get(request.key)
        if cached is not None:
            return cached
        image = pages.render_viewport(
            self._page(request.path), request.vx, request.vy,
            request.scale, request.vw, request.vh, request.mode,
        )
        self._cache[request.key] = image
        while len(self._cache) > RENDER_CACHE:
            self._cache.pop(next(iter(self._cache)))
        return image


class ReviewApp(tk.Tk):
    """The whole application."""

    def __init__(self, folder: str | None = None, workbook: str | None = None,
                 cache_dir: str | None = None, reviewer: str | None = None):
        super().__init__()
        self.title("Geophysical Log Reviewer")
        self.geometry("1500x950")   # the size it restores to when un-maximised
        self.minsize(1200, 760)
        try:
            self.state("zoomed")    # open filling the screen; Windows/most X11 WMs
        except tk.TclError:         # pragma: no cover - other window managers
            self.attributes("-zoomed", True)

        self.all_paths: list[str] = []
        self.paths: list[str] = []  # active whole-dataset or subset view
        self.view_mode = "all"
        self.infos: dict[str, pages.PageInfo] = {}
        self.index: int = -1
        self.folder: str = ""
        self.store: ReviewStore | None = None
        self.types: list[str] = []
        self.log_types: list[str] = load_log_types()
        self.reviewer = reviewer.strip() if reviewer else ""
        self._reviewer_override = reviewer is not None
        self._baseline_draft: _EntryDraft | None = None
        self._draft_has_stamp: bool | None = None
        self._loading_draft = False
        self._stamp_rows: list[dict[str, object]] = []
        self._entry_existed = False
        self._tree_selection_guard = False
        self._update_results: "queue.Queue" = queue.Queue()
        self._update_check_running = False
        self._auto_update_scheduled = False
        self._update_progress = None
        self.cache_dir = os.path.abspath(cache_dir or _default_cache_dir())
        os.makedirs(self.cache_dir, exist_ok=True)

        self.scale_value = 1.0
        self.vx = 0.0
        self.vy = 0.0
        # "width" / "page" survive a window resize; an explicit zoom clears it.
        self._fit_mode: str | None = "width"
        self._photo = None
        self._image_id = None
        self._token = 0
        self._drag: tuple[int, int, float, float] | None = None
        self._render_job = None

        self._results: "queue.Queue" = queue.Queue()
        self._worker = _RenderWorker(self._results, self.cache_dir)
        self._worker.start()

        self._build_ui()
        self._bind_keys()
        self.after(40, self._poll_results)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if _frozen() and not self._reviewer_override:
            self._auto_update_scheduled = True
            self.after(1500, self._auto_check_for_updates)

        start_folder = folder or _default_folder()
        if start_folder and os.path.isdir(start_folder):
            if not self.open_folder(start_folder, workbook):
                self._show_welcome()
        else:
            self._show_welcome()
            self.after(200, self._guided_start)

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Toolbar.TLabel", padding=(6, 2))

        # Two explicit lines: what is being reviewed, and what file the answers
        # end up in. A reviewer should never have to guess where the work is going.
        bar = ttk.Frame(self, padding=(8, 6))
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Reviewing:", font=("Segoe UI", 9, "bold")
                  ).grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value="no folder open")
        ttk.Label(bar, textvariable=self.folder_var, foreground="#333"
                  ).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(bar, text="Change folder…", command=self._pick_folder
                   ).grid(row=0, column=2, sticky="e", padx=(6, 0))
        self.progress_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.progress_var, font=("Segoe UI", 10, "bold")
                  ).grid(row=0, column=3, sticky="e", padx=(16, 0))

        ttk.Label(bar, text="Saving to Excel:", font=("Segoe UI", 9, "bold"),
                  foreground="#1a5c2f").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.workbook_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.workbook_var, foreground="#1a5c2f"
                  ).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        actions = ttk.Frame(bar)
        actions.grid(row=1, column=2, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(actions, text="Change…", command=self._pick_workbook).pack(side="left")
        ttk.Button(actions, text="Show in Explorer", command=self._reveal_workbook
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Save now  (Ctrl+S)", command=self.save
                   ).pack(side="left", padx=(6, 0))
        ttk.Label(actions, text="Reviewer:", font=("Segoe UI", 9, "bold")
                  ).pack(side="left", padx=(14, 3))
        self.reviewer_var = tk.StringVar(value=self.reviewer or "not selected")
        ttk.Label(actions, textvariable=self.reviewer_var, foreground="#333"
                  ).pack(side="left")
        ttk.Button(actions, text="Change", command=self._change_reviewer
                   ).pack(side="left", padx=(5, 0))
        self.update_button = ttk.Button(
            actions, text="Check for updates", command=lambda: self._check_for_updates(True)
        )
        self.update_button.pack(side="left", padx=(10, 0))
        self.saved_var = tk.StringVar(value="not saved yet")
        ttk.Label(actions, textvariable=self.saved_var, foreground="#555", width=26,
                  anchor="e").pack(side="left", padx=(8, 0))

        # --- list ------------------------------------------------------------
        left = ttk.Frame(self, padding=(8, 0, 4, 0))
        left.grid(row=1, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        filter_row = ttk.Frame(left)
        filter_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(filter_row, text="show:").pack(side="left")
        self.filter_var = tk.StringVar(value="all")
        for label, value in (("all", "all"), ("to do", "todo"), ("done", "done")):
            ttk.Radiobutton(filter_row, text=label, value=value,
                            variable=self.filter_var,
                            command=self._refresh_list).pack(side="left", padx=(4, 0))
        ttk.Separator(filter_row, orient="vertical").pack(
            side="left", fill="y", padx=(10, 8)
        )
        ttk.Label(filter_row, text="evaluate:").pack(side="left")
        self.scope_var = tk.StringVar(value="all")
        ttk.Radiobutton(
            filter_row, text="whole dataset", value="all", variable=self.scope_var,
            command=lambda: self._switch_scope("all"),
        ).pack(side="left", padx=(4, 0))
        self.subset_label_var = tk.StringVar(value="subset")
        self.subset_scope_button = ttk.Radiobutton(
            filter_row, textvariable=self.subset_label_var, value="subset",
            variable=self.scope_var, command=lambda: self._switch_scope("subset"),
            state="disabled",
        )
        self.subset_scope_button.pack(side="left", padx=(4, 0))
        ttk.Button(
            filter_row, text="Evaluate a subset…", command=self._evaluate_subset
        ).pack(side="left", padx=(8, 0))

        columns = ("rev", "file", "stamp", "type", "notes")
        self.tree = ttk.Treeview(left, columns=columns, show="headings",
                                 selectmode="browse", height=30)
        self.tree.heading("rev", text="Reviewed")
        self.tree.heading("file", text="Log file")
        self.tree.heading("stamp", text="Stamp")
        self.tree.heading("type", text="Type")
        self.tree.heading("notes", text="Note")
        self.tree.column("rev", width=70, anchor="center", stretch=False)
        self.tree.column("file", width=290, anchor="w")
        self.tree.column("stamp", width=60, anchor="center", stretch=False)
        self.tree.column("type", width=90, anchor="w", stretch=False)
        self.tree.column("notes", width=220, anchor="w", stretch=False)
        self.tree.tag_configure("done", foreground=DONE_FG)
        self.tree.tag_configure("warn", foreground=WARN_FG)
        self.tree.tag_configure("todo", foreground=TODO_FG)
        self.tree.grid(row=1, column=0, sticky="nsew")
        tree_y_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        tree_x_scroll = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y_scroll.set,
                            xscrollcommand=tree_x_scroll.set)
        tree_y_scroll.grid(row=1, column=1, sticky="ns")
        tree_x_scroll.grid(row=2, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # --- viewer ----------------------------------------------------------
        right = ttk.Frame(self, padding=(4, 0, 8, 0))
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(right, bg="#5a5f66", highlightthickness=1,
                                highlightbackground="#333", cursor="fleur")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.page_scroll = ttk.Scrollbar(right, orient="vertical",
                                         command=self._on_vertical_scroll)
        self.page_scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.bind("<Configure>", lambda e: self._on_resize())
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)

        zoom_row = ttk.Frame(right, padding=(0, 6))
        zoom_row.grid(row=1, column=0, columnspan=2, sticky="ew")
        for text, command in (
            ("−", lambda: self._zoom_step(-1)),
            ("+", lambda: self._zoom_step(1)),
            ("Fit width (W)", self.fit_width),
            ("Whole page (F)", self.fit_page),
            ("1:1", self.zoom_actual),
            ("Top (T)", self.goto_top),
            ("Bottom (B)", self.goto_bottom),
            ("Show TIFF in Explorer", self._reveal_current_tiff),
        ):
            ttk.Button(zoom_row, text=text, command=command).pack(side="left", padx=(0, 4))
        self.view_var = tk.StringVar(value="")
        ttk.Label(zoom_row, textvariable=self.view_var, foreground="#333").pack(side="left", padx=(12, 0))

        # --- draft-and-commit workflow --------------------------------------
        workflow = ttk.Frame(self, padding=(10, 8), height=190)
        workflow.grid(row=2, column=0, columnspan=2, sticky="ew")
        workflow.grid_propagate(False)
        workflow.columnconfigure(0, weight=4, minsize=470)
        workflow.columnconfigure(1, weight=0, minsize=34)
        workflow.columnconfigure(2, weight=2, minsize=250)
        workflow.columnconfigure(3, weight=0, minsize=34)
        workflow.columnconfigure(4, weight=4, minsize=390)
        workflow.rowconfigure(0, weight=1)

        stamp = ttk.LabelFrame(workflow, text="1. Stamp review", padding=(10, 8))
        stamp.grid(row=0, column=0, sticky="nsew")
        stamp.columnconfigure(1, weight=1)
        ttk.Label(stamp, text="Has stamp?", font=("Segoe UI", 10, "bold")
                  ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        choice = ttk.Frame(stamp)
        choice.grid(row=0, column=1, sticky="w")
        self.yes_button = tk.Button(
            choice, text="Yes  (Y)", width=9, command=lambda: self.set_verdict(True)
        )
        self.yes_button.pack(side="left")
        self.no_button = tk.Button(
            choice, text="No  (N)", width=9, command=lambda: self.set_verdict(False)
        )
        self.no_button.pack(side="left", padx=(6, 0))

        self.stamp_controls = ttk.Frame(stamp)
        self.stamp_controls.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        self.stamp_controls.columnconfigure(0, weight=1)
        self.stamp_rows_frame = ttk.Frame(self.stamp_controls)
        self.stamp_rows_frame.grid(row=0, column=0, sticky="ew")
        self.stamp_rows_frame.columnconfigure(0, weight=1)
        stamp_actions = ttk.Frame(self.stamp_controls)
        stamp_actions.grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.add_stamp_button = ttk.Button(
            stamp_actions, text="+ Add another stamp", command=self._add_stamp_row
        )
        self.add_stamp_button.pack(side="left")
        ttk.Button(stamp_actions, text="+ Add stamp type…", command=self._add_type
                   ).pack(side="left", padx=(6, 0))

        ttk.Label(workflow, text="→", font=("Segoe UI", 20, "bold"),
                  foreground="#1a5c2f").grid(row=0, column=1)

        log_section = ttk.LabelFrame(workflow, text="2. Log types (optional)",
                                     padding=(10, 8))
        log_section.grid(row=0, column=2, sticky="nsew")
        log_section.rowconfigure(0, weight=1)
        log_section.columnconfigure(0, weight=1)
        self.log_type_canvas = tk.Canvas(log_section, highlightthickness=0, height=100)
        self.log_type_canvas.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_section, orient="vertical",
                                   command=self.log_type_canvas.yview)
        self.log_type_canvas.configure(yscrollcommand=log_scroll.set)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_type_frame = ttk.Frame(self.log_type_canvas)
        self._log_type_window = self.log_type_canvas.create_window(
            0, 0, anchor="nw", window=self.log_type_frame
        )
        self.log_type_vars: dict[str, tk.BooleanVar] = {}
        self.log_type_frame.bind(
            "<Configure>",
            lambda _event: self.log_type_canvas.configure(
                scrollregion=self.log_type_canvas.bbox("all")
            ),
        )
        self.log_type_canvas.bind(
            "<Configure>",
            lambda event: self.log_type_canvas.itemconfigure(
                self._log_type_window, width=event.width
            ),
        )
        for value in list(self.log_types):
            self._ensure_log_type_option(value)

        ttk.Label(workflow, text="→", font=("Segoe UI", 20, "bold"),
                  foreground="#1a5c2f").grid(row=0, column=3)

        details = ttk.LabelFrame(workflow, text="3. Notes and entry", padding=(10, 8))
        details.grid(row=0, column=4, sticky="nsew")
        details.columnconfigure(0, weight=1)
        ttk.Label(details, text="Notes (optional):").grid(row=0, column=0, sticky="w")
        self.notes_var = tk.StringVar(value="")
        self.notes_entry = ttk.Entry(details, textvariable=self.notes_var)
        self.notes_entry.grid(row=1, column=0, sticky="ew", pady=(3, 6))
        self.notes_var.trace_add("write", self._on_draft_changed)
        self.entry_validation_var = tk.StringVar(value="Make a change to add an entry.")
        ttk.Label(details, textvariable=self.entry_validation_var,
                  foreground="#8a5a00").grid(row=2, column=0, sticky="w")
        entry_actions = ttk.Frame(details)
        entry_actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.submit_button = tk.Button(
            entry_actions, text="Add entry  →", command=self.submit_entry,
            state="disabled", font=("Segoe UI", 11, "bold"),
            foreground="white", background="#1a7f37",
            activeforeground="white", activebackground="#146c2e",
            disabledforeground="#d5e5d9",
            relief="flat", borderwidth=0, padx=18, pady=8, cursor="hand2",
        )
        self.submit_button.pack(side="left")
        ttk.Button(entry_actions, text="Next unreviewed  (Space)",
                   command=self.next_unreviewed).pack(side="right")

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, padding=(10, 4),
                  foreground="#333", anchor="w").grid(row=3, column=0, columnspan=2, sticky="ew")

        self.rowconfigure(1, weight=1)
        self.columnconfigure(1, weight=1)

        self.welcome = ttk.Frame(self, padding=40)
        ttk.Label(self.welcome, text="Geophysical Log Reviewer",
                  font=("Segoe UI", 24, "bold")
                  ).pack(pady=(50, 10))
        ttk.Label(
            self.welcome,
            text="No review is open. Start by selecting the folder containing "
                 "the geophysical log TIFFs.",
            font=("Segoe UI", 11),
            wraplength=620,
            justify="center",
        ).pack(pady=(0, 18))
        ttk.Button(self.welcome, text="Start review…", command=self._pick_folder
                   ).pack()

    def _bind_keys(self) -> None:
        self.bind_all("<Key>", self._on_key)
        self.bind_all("<Control-s>", lambda e: (self.save(), "break")[1])

    # -------------------------------------------------------------- folders

    def _show_welcome(self) -> None:
        self.welcome.grid(row=1, column=0, columnspan=2, rowspan=2, sticky="nsew")
        self.welcome.tkraise()

    def _hide_welcome(self) -> None:
        self.welcome.grid_remove()

    def _guided_start(self) -> None:
        messagebox.showinfo(
            "Select geophysical logs",
            "You will now be prompted to select the folder containing the "
            "geophysical log images.",
            parent=self,
        )
        self._pick_folder()

    def _pick_folder(self) -> bool:
        folder = filedialog.askdirectory(title="Folder of scanned logs",
                                        initialdir=self.folder or os.getcwd())
        if not folder:
            if not self.folder:
                self._show_welcome()
            return False
        try:
            found = pages.list_images(folder)
        except OSError as exc:
            messagebox.showerror("Cannot read folder", str(exc), parent=self)
            return False
        if not found:
            messagebox.showwarning(
                "Nothing to review",
                f"No images ({', '.join(pages.IMAGE_EXTS)}) in\n{folder}",
                parent=self,
            )
            return False
        return self._choose_workbook(folder, found)

    def _choice_dialog(
        self, title: str, message: str, choices: list[tuple[str, str]], cancel: str
    ) -> str:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        result = {"value": cancel}
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, justify="left", wraplength=620
                  ).pack(anchor="w", pady=(0, 16))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def finish(value: str) -> None:
            result["value"] = value
            dialog.destroy()

        for index, (label, value) in enumerate(choices):
            ttk.Button(buttons, text=label, command=lambda item=value: finish(item)
                       ).pack(side="left", padx=(0 if index == 0 else 7, 0))
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(cancel))
        dialog.bind("<Escape>", lambda _event: finish(cancel))
        dialog.grab_set()
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        self.wait_window(dialog)
        return result["value"]

    def _ask_workbook_action(self) -> str:
        return self._choice_dialog(
            "Select review workbook",
            "Choose how this review should store its entries.",
            [("Open existing…", "existing"), ("Create new…", "new"),
             ("Back", "back")],
            "back",
        )

    def _choose_workbook(self, folder: str, images: list[str]) -> bool:
        """Recoverable Existing/New/Back workflow; file-dialog cancel loops here."""
        default = _default_workbook(folder)
        default_dir = os.path.dirname(default)
        try:
            os.makedirs(default_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Cannot prepare review folder", f"{default_dir}\n\n{exc}", parent=self
            )
            return False
        while True:
            action = self._ask_workbook_action()
            if action == "back":
                if not self.folder:
                    self._show_welcome()
                return False
            if action == "existing":
                path = filedialog.askopenfilename(
                    title="Open an existing LogReview workbook",
                    filetypes=[("Excel workbook", "*.xlsx")],
                    initialdir=default_dir,
                    parent=self,
                )
                if not path:
                    continue
                try:
                    validate_review_workbook(
                        path, {os.path.basename(image) for image in images}
                    )
                except InvalidWorkbook as exc:
                    messagebox.showerror(
                        "Not a valid LogReview workbook",
                        f"{path}\n\n{exc}\n\nPlease select a different workbook.",
                        parent=self,
                    )
                    continue
            else:
                path = filedialog.asksaveasfilename(
                    title="Create a new LogReview workbook",
                    defaultextension=".xlsx",
                    filetypes=[("Excel workbook", "*.xlsx")],
                    initialdir=default_dir,
                    initialfile=os.path.basename(default),
                    confirmoverwrite=False,
                    parent=self,
                )
                if not path:
                    continue
                if os.path.exists(path):
                    messagebox.showerror(
                        "File already exists",
                        "Choose a new file name, or open it as an existing workbook.",
                        parent=self,
                    )
                    continue
            if self.open_folder(folder, path):
                return True

    def _pick_workbook(self) -> None:
        if not self.folder:
            return
        self._choose_workbook(self.folder, self.all_paths)

    def _prompt_reviewer(self, default: str = "") -> str | None:
        while True:
            value = simpledialog.askstring(
                "Reviewer",
                "Enter the initials or name responsible for entries in this session:",
                initialvalue=default,
                parent=self,
            )
            if value is None:
                return None
            value = value.strip()
            if value:
                return value
            messagebox.showwarning("Reviewer required", "Enter a nonblank reviewer.",
                                   parent=self)

    def _change_reviewer(self) -> None:
        value = self._prompt_reviewer(self.reviewer)
        if value is not None:
            self.reviewer = value
            self.reviewer_var.set(value)

    def _reveal_workbook(self) -> None:
        """Open the workbook's folder in Explorer, selecting the file if it exists."""
        if self.store is None:
            return
        folder = os.path.dirname(self.store.path)
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # noqa: S606 - Windows shell, user-initiated
        except (AttributeError, OSError) as exc:  # pragma: no cover - non-Windows
            messagebox.showinfo("Workbook location", f"{self.store.path}\n\n{exc}")

    def _reveal_current_tiff(self) -> None:
        """Open Explorer with the currently displayed source image selected."""
        path = self.current_path
        if not path or not os.path.exists(path):
            messagebox.showwarning("TIFF unavailable", "No source image is currently open.",
                                   parent=self)
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", f"/select,{os.path.normpath(path)}"])
            else:  # pragma: no cover - standalone product targets Windows
                os.startfile(os.path.dirname(path))
        except (AttributeError, OSError) as exc:
            messagebox.showerror("Could not open Explorer", f"{path}\n\n{exc}",
                                 parent=self)

    def _format_names(self, names: tuple[str, ...], limit: int = 8) -> str:
        shown = "\n".join(f"  • {name}" for name in names[:limit])
        if len(names) > limit:
            shown += f"\n  • …and {len(names) - limit} more"
        return shown

    def _handle_reconciliation(
        self, book: str, inspection: WorkbookInspection
    ) -> bool:
        if inspection.missing_names:
            action = self._choice_dialog(
                "Workbook is missing log rows",
                f"{len(inspection.missing_names)} TIFF(s) are not listed in the workbook. "
                "They will be recreated as blank, incomplete entries; deleted review "
                "content cannot be recovered.\n\n"
                + self._format_names(inspection.missing_names),
                [("Continue", "continue"), ("Back", "back")],
                "back",
            )
            if action != "continue":
                return False

        while inspection.extra_names:
            action = self._choice_dialog(
                "Workbook contains rows outside this folder",
                f"{len(inspection.extra_names)} workbook row(s) have no matching TIFF in "
                "the selected folder. A renamed TIFF appears here as an extra row and a "
                "missing row; LogReview will not guess a match.\n\n"
                + self._format_names(inspection.extra_names),
                [("Remove rows", "remove"), ("Keep rows", "keep"), ("Back", "back")],
                "back",
            )
            if action == "back":
                return False
            if action == "keep":
                return True
            if self._remove_extra_rows(book, inspection):
                return True
        return True

    def _remove_extra_rows(self, book: str, inspection: WorkbookInspection) -> bool:
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        root, extension = os.path.splitext(book)
        backup = f"{root}.pre_reconcile_{stamp}{extension}"
        suffix = 2
        while os.path.exists(backup):
            backup = f"{root}.pre_reconcile_{stamp}_{suffix}{extension}"
            suffix += 1
        try:
            shutil.copy2(book, backup)
            store = ReviewStore(book)
            store.load()
            store.remove_records(inspection.extra_names)
            store.dirty = True
            remaining = [name for name in inspection.workbook_names
                         if name.casefold() not in {
                             extra.casefold() for extra in inspection.extra_names
                         }]
            store.save(remaining)
        except Exception as exc:
            messagebox.showerror(
                "Could not reconcile workbook",
                f"The original workbook was not changed.\n\n{exc}",
                parent=self,
            )
            return False
        messagebox.showinfo(
            "Rows removed",
            f"Removed {len(inspection.extra_names)} row(s).\n\nBackup: {backup}",
            parent=self,
        )
        return True

    def open_folder(self, folder: str | None, workbook: str | None = None) -> bool:
        if folder is None:
            folder = filedialog.askdirectory(title="Folder of scanned logs",
                                            initialdir=os.getcwd())
            if not folder:
                self._show_welcome()
                return False
        try:
            found = pages.list_images(folder)
        except OSError as exc:
            messagebox.showerror("Cannot read folder", str(exc))
            return False
        if not found:
            messagebox.showwarning("Nothing to review",
                                   f"No images ({', '.join(pages.IMAGE_EXTS)}) in\n{folder}")
            return False
        if self.index >= 0 and not self._resolve_pending_draft():
            return False

        # A folder owns its own default workbook. Reusing the previous folder's
        # workbook here would silently mix two review corpora after "Change folder".
        folder_abs = os.path.abspath(folder)
        book = workbook or _default_workbook(folder_abs)
        if os.path.exists(book):
            try:
                inspection = validate_review_workbook(
                    book, {os.path.basename(path) for path in found}
                )
            except InvalidWorkbook as exc:
                messagebox.showerror(
                    "Not a valid LogReview workbook", f"{book}\n\n{exc}", parent=self
                )
                return False
            if not self._handle_reconciliation(book, inspection):
                return False
        new_store = ReviewStore(book)
        try:
            new_store.load()
        except Exception as exc:
            messagebox.showerror("Cannot read workbook", f"{book}\n\n{exc}")
            return False

        reviewer = self.reviewer if self._reviewer_override else self._prompt_reviewer(
            new_store.most_recent_reviewer()
        )
        if reviewer is None:
            return False

        # Commit the switch only after both inputs have been checked. A rejected
        # workbook must leave the currently open review completely untouched.
        self.folder = folder_abs
        self.all_paths = found
        self.paths = list(found)
        self.view_mode = "all"
        self.scope_var.set("all")
        self.infos = {}
        self.index = -1
        self.store = new_store
        self.reviewer = reviewer
        self.reviewer_var.set(reviewer)
        for path in self.all_paths:  # seed rows so the workbook lists every file
            self.store.record_for(path, pages.api14_from_name(path))
        self.folder_var.set(f"{self.folder}   ({len(self.all_paths)} images)")
        self.workbook_var.set(self.store.path)
        self.saved_var.set("saved earlier" if os.path.exists(self.store.path)
                           else "not saved yet")

        types_file = stamp_types_path(self.store.path)
        self.types = load_stamp_types(types_file)
        for used in self.store.stamp_types_in_use():
            if used.lower() not in {t.lower() for t in self.types}:
                self.types.append(used)
        self._update_stamp_choices()
        self._refresh_scope_controls()

        self.title(f"Geophysical Log Reviewer - {os.path.basename(self.folder)}")
        self._hide_welcome()
        self._refresh_list()
        first = self._first_unreviewed()
        self.select_index(first if first is not None else 0)
        return True

    # -------------------------------------------------------------- subsets

    @staticmethod
    def _leading_api(path: str) -> str:
        """Return a leading 14-digit API, never a number later in the name."""
        name = os.path.basename(path)
        api = name[:14]
        if len(api) != 14 or not api.isdigit():
            return ""
        if len(name) > 14 and name[14].isdigit():
            return ""
        return api

    def _subset_paths(self) -> list[str]:
        if self.store is None or not self.store.subset_apis:
            return []
        wanted = set(self.store.subset_apis)
        return [path for path in self.all_paths if self._leading_api(path) in wanted]

    def _refresh_scope_controls(self) -> None:
        requested = len(self.store.subset_apis) if self.store is not None else 0
        matched = len(self._subset_paths())
        self.subset_label_var.set(
            f"subset ({matched} log{'s' if matched != 1 else ''})"
            if requested else "subset"
        )
        self.subset_scope_button.configure(state="normal" if matched else "disabled")
        if self.view_mode == "subset" and not matched:
            self.view_mode = "all"
            self.scope_var.set("all")
            self.paths = list(self.all_paths)

    def _switch_scope(self, mode: str) -> bool:
        """Switch navigation/progress between all TIFFs and the saved subset."""
        if mode not in {"all", "subset"}:
            self.scope_var.set(self.view_mode)
            return False
        target = list(self.all_paths) if mode == "all" else self._subset_paths()
        if mode == self.view_mode and target == self.paths:
            self.scope_var.set(self.view_mode)
            return True
        if mode == "subset" and not target:
            self.scope_var.set(self.view_mode)
            messagebox.showwarning(
                "Subset has no matching logs",
                "Create a subset containing API numbers found at the beginning of "
                "the loaded TIFF filenames.",
                parent=self,
            )
            return False
        if not self._resolve_pending_draft():
            self.scope_var.set(self.view_mode)
            return False

        previous_path = self.current_path
        self.view_mode = mode
        self.scope_var.set(mode)
        self.paths = target
        self.index = -1
        self._refresh_list()
        next_index = None
        if previous_path:
            previous_key = os.path.normcase(os.path.abspath(previous_path))
            next_index = next(
                (i for i, path in enumerate(self.paths)
                 if os.path.normcase(os.path.abspath(path)) == previous_key),
                None,
            )
        if next_index is None:
            next_index = self._first_unreviewed()
        self.select_index(next_index if next_index is not None else 0)
        return True

    def _evaluate_subset(self) -> None:
        """Import one API per line, save membership, and enter subset mode."""
        if self.store is None or not self.all_paths:
            messagebox.showwarning(
                "No review is open",
                "Open a folder of geophysical logs before creating a subset.",
                parent=self,
            )
            return
        if not self._resolve_pending_draft():
            return
        source = filedialog.askopenfilename(
            title="Select a text file containing one API number per line",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialdir=self.folder or os.getcwd(),
            parent=self,
        )
        if not source:
            return
        try:
            with open(source, encoding="utf-8-sig") as handle:
                raw_lines = handle.read().splitlines()
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("Cannot read API list", f"{source}\n\n{exc}", parent=self)
            return

        requested, duplicates, invalid = _parse_subset_api_lines(raw_lines)
        if invalid:
            shown = "\n".join(
                f"Line {line}: {value}" for line, value in invalid[:10]
            )
            if len(invalid) > 10:
                shown += f"\n…and {len(invalid) - 10} more"
            messagebox.showerror(
                "Invalid API list",
                "Each nonblank line must contain exactly one 14-digit API number.\n\n"
                + shown,
                parent=self,
            )
            return
        if not requested:
            messagebox.showwarning(
                "Empty API list", "The selected file contains no API numbers.", parent=self
            )
            return

        requested_set = set(requested)
        matched_paths = [
            path for path in self.all_paths if self._leading_api(path) in requested_set
        ]
        matched_apis = {self._leading_api(path) for path in matched_paths}
        unmatched = [api for api in requested if api not in matched_apis]
        if not matched_paths:
            messagebox.showwarning(
                "No matching logs",
                "None of the requested APIs match the beginning of a loaded TIFF filename. "
                "The existing subset was not changed.",
                parent=self,
            )
            return

        summary = (
            f"Requested APIs: {len(requested)}\n"
            f"Matched APIs: {len(matched_apis)}\n"
            f"Matched TIFFs: {len(matched_paths)}\n"
            f"Unmatched APIs retained in the subset sheet: {len(unmatched)}\n"
            f"Duplicate lines ignored: {duplicates}"
        )
        if unmatched:
            summary += "\n\nUnmatched:\n" + "\n".join(
                f"  {api}" for api in unmatched[:8]
            )
            if len(unmatched) > 8:
                summary += f"\n  …and {len(unmatched) - 8} more"
        action = self._choice_dialog(
            "Create evaluation subset",
            summary,
            [(
                "Replace subset" if self.store.subset_apis else "Create subset",
                "continue",
            ), ("Cancel", "cancel")],
            "cancel",
        )
        if action != "continue":
            return

        old_subset = list(self.store.subset_apis)
        old_dirty = self.store.dirty
        self.store.replace_subset(requested)
        if not self._save_store(quiet=True):
            self.store.subset_apis = old_subset
            self.store.dirty = old_dirty
            self._refresh_scope_controls()
            return
        self._refresh_scope_controls()
        self._switch_scope("subset")
        self.status_var.set(
            f"Evaluating {len(matched_paths)} TIFF(s) from {len(matched_apis)} API(s)."
        )

    # ----------------------------------------------------------------- list

    def _visible_indices(self) -> list[int]:
        mode = self.filter_var.get()
        out = []
        for i, path in enumerate(self.paths):
            record = self.store.records.get(os.path.basename(path))
            reviewed = bool(record and record.reviewed)
            if mode == "todo" and reviewed:
                continue
            if mode == "done" and not reviewed:
                continue
            out.append(i)
        return out

    def _refresh_list(self) -> None:
        selected = self.index
        self.tree.delete(*self.tree.get_children())
        for i in self._visible_indices():
            self.tree.insert("", "end", iid=str(i), values=self._row_values(i),
                             tags=(self._row_tag(i),))
        if selected >= 0 and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))
        self._refresh_progress()

    def _row_values(self, i: int) -> tuple:
        record = self.store.records.get(os.path.basename(self.paths[i]))
        if record is None or not record.has_entry:
            return ("", os.path.basename(self.paths[i]), "", "", "")
        mark = WARN if record.incomplete else CHECK
        stamp = "" if record.has_stamp is None else ("yes" if record.has_stamp else "no")
        return (mark, record.file_name, stamp, record.stamp_type, record.notes)

    def _row_tag(self, i: int) -> str:
        record = self.store.records.get(os.path.basename(self.paths[i]))
        if record is None or not record.has_entry:
            return "todo"
        return "warn" if record.incomplete else "done"

    def _refresh_row(self, i: int) -> None:
        if self.tree.exists(str(i)):
            self.tree.item(str(i), values=self._row_values(i), tags=(self._row_tag(i),))
        self._refresh_progress()

    def _refresh_progress(self) -> None:
        names = [os.path.basename(path) for path in self.paths]
        reviewed, with_stamp, incomplete = self.store.counts(names)
        total = len(self.paths)
        scope = " in subset" if self.view_mode == "subset" else ""
        text = f"{reviewed} / {total} reviewed{scope}   |   {with_stamp} with a stamp"
        if incomplete:
            text += f"   |   {incomplete} incomplete entries"
        self.progress_var.set(text)
        if self.store.dirty:
            self.saved_var.set("UNSAVED - press Ctrl+S")

    def _first_unreviewed(self) -> int | None:
        for i, path in enumerate(self.paths):
            record = self.store.records.get(os.path.basename(path))
            if record is None or not record.reviewed:
                return i
        return None

    def _on_tree_select(self, _event=None) -> None:
        if self._tree_selection_guard:
            return
        selection = self.tree.selection()
        if selection:
            index = int(selection[0])
            if index != self.index:
                if not self.select_index(index, from_tree=True):
                    self._tree_selection_guard = True
                    try:
                        if self.index >= 0 and self.tree.exists(str(self.index)):
                            self.tree.selection_set(str(self.index))
                    finally:
                        self._tree_selection_guard = False

    # ----------------------------------------------------------------- pages

    @property
    def current_path(self) -> str | None:
        if 0 <= self.index < len(self.paths):
            return self.paths[self.index]
        return None

    @property
    def current_record(self):
        path = self.current_path
        return self.store.record_for(path) if path else None

    def select_index(self, index: int, from_tree: bool = False) -> bool:
        if not self.paths:
            return False
        index = max(0, min(index, len(self.paths) - 1))
        if index != self.index and not self._resolve_pending_draft():
            return False
        self.index = index
        path = self.paths[index]

        info = self.infos.get(path)
        if info is None:
            try:
                info = pages.probe(path)
            except Exception as exc:
                self.status_var.set(f"cannot open {os.path.basename(path)}: {exc}")
                return False
            self.infos[path] = info
        record = self.store.record_for(path, info.api14 or "")

        if not from_tree:
            if not self.tree.exists(str(index)):
                self.filter_var.set("all")
                self._refresh_list()
            if self.tree.exists(str(index)):
                self.tree.selection_set(str(index))
                self.tree.see(str(index))

        self._load_entry_draft(record)

        width_in, height_in = info.size_in
        self.status_var.set(
            f"[{index + 1}/{len(self.paths)}]  {info.name}   "
            f"{info.width} x {info.height} px   {width_in:.1f} x {height_in:.1f} in "
            f"@ {info.dpi:g} dpi   {info.megapixels:.0f} MP"
            + (f"   API {info.api14}" if info.api14 else "")
        )
        self.vy = 0.0  # a new page opens at its header, where the stamp usually is
        self.fit_width()
        self.canvas.focus_set()
        return True

    # ---------------------------------------------------------------- zoom

    def _viewport(self) -> tuple[int, int]:
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def _info(self) -> pages.PageInfo | None:
        path = self.current_path
        return self.infos.get(path) if path else None

    def _on_resize(self) -> None:
        """Keep whatever fit is active; otherwise just repaint at the new size."""
        if self._fit_mode == "width":
            self.fit_width()
        elif self._fit_mode == "page":
            self.fit_page()
        else:
            self._schedule_render()

    def fit_width(self) -> None:
        info = self._info()
        if not info:
            return
        vw, _ = self._viewport()
        self.scale_value = vw / info.width
        self.vx = 0.0
        self._fit_mode = "width"
        self._schedule_render()

    def fit_page(self) -> None:
        info = self._info()
        if not info:
            return
        vw, vh = self._viewport()
        self.scale_value = min(vw / info.width, vh / info.height)
        self.vx = -(vw / self.scale_value - info.width) / 2
        self.vy = 0.0
        self._fit_mode = "page"
        self._schedule_render()

    def zoom_actual(self) -> None:
        self._set_scale(1.0)

    def goto_top(self) -> None:
        self.vy = 0.0
        self._schedule_render()

    def goto_bottom(self) -> None:
        info = self._info()
        if not info:
            return
        _, vh = self._viewport()
        self.vy = max(0.0, info.height - vh / self.scale_value)
        self._schedule_render()

    def _zoom_step(self, direction: int, anchor: tuple[int, int] | None = None) -> None:
        current = self.scale_value
        if direction > 0:
            nxt = next((s for s in ZOOM_LEVELS if s > current * 1.001), ZOOM_LEVELS[-1])
        else:
            # Fit-whole-page on a 528,000-row log sits below the bottom rung, so a
            # missing rung means "already as far out as it goes", not "jump up".
            nxt = next((s for s in reversed(ZOOM_LEVELS) if s < current * 0.999), current)
        self._set_scale(nxt, anchor)

    def _set_scale(self, scale: float, anchor: tuple[int, int] | None = None) -> None:
        info = self._info()
        if not info:
            return
        vw, vh = self._viewport()
        ax, ay = anchor or (vw // 2, vh // 2)
        # Keep the page point under the anchor where it is.
        px = self.vx + ax / self.scale_value
        py = self.vy + ay / self.scale_value
        self.scale_value = scale
        self.vx = px - ax / scale
        self.vy = py - ay / scale
        self._fit_mode = None
        self._clamp()
        self._schedule_render()

    def _clamp(self) -> None:
        info = self._info()
        if not info:
            return
        vw, vh = self._viewport()
        page_w, page_h = vw / self.scale_value, vh / self.scale_value
        self.vx = max(-page_w / 2, min(self.vx, info.width - page_w / 2))
        self.vy = max(-page_h / 2, min(self.vy, info.height - page_h / 2))

    # ---------------------------------------------------------------- panning

    def _on_drag_start(self, event) -> None:
        self.canvas.focus_set()
        self._drag = (event.x, event.y, self.vx, self.vy)

    def _on_drag_move(self, event) -> None:
        if not self._drag or self._image_id is None:
            return
        x0, y0, vx0, vy0 = self._drag
        dx, dy = event.x - x0, event.y - y0
        self.vx = vx0 - dx / self.scale_value
        self.vy = vy0 - dy / self.scale_value
        self._clamp()
        self._update_vertical_scrollbar()
        # Instant feedback: slide the already-rendered bitmap, re-render on release.
        self.canvas.coords(self._image_id, dx, dy)

    def _on_drag_end(self, _event) -> None:
        self._drag = None
        self._schedule_render()

    def _on_wheel(self, event) -> str:
        self._zoom_step(1 if event.delta > 0 else -1, (event.x, event.y))
        return "break"

    def _on_shift_wheel(self, event) -> str:
        vw, _ = self._viewport()
        self.vx -= (event.delta / 120.0) * (vw * 0.25) / self.scale_value
        self._clamp()
        self._schedule_render()
        return "break"

    def _on_ctrl_wheel(self, event) -> str:
        self._zoom_step(1 if event.delta > 0 else -1, (event.x, event.y))
        return "break"

    def _on_vertical_scroll(self, *args) -> None:
        info = self._info()
        if not info or not args:
            return
        _, vh = self._viewport()
        visible = vh / self.scale_value
        maximum = max(0.0, info.height - visible)
        if args[0] == "moveto":
            # Tk reports the requested top as a fraction of the full document,
            # matching the fractions passed to Scrollbar.set below.
            self.vy = float(args[1]) * info.height
        elif args[0] == "scroll":
            amount = int(args[1])
            step = visible * (0.9 if args[2] == "pages" else 0.1)
            self.vy += amount * step
        self._clamp()
        self._schedule_render()

    def _update_vertical_scrollbar(self) -> None:
        info = self._info()
        if not info:
            self.page_scroll.set(0.0, 1.0)
            return
        _, vh = self._viewport()
        visible = vh / self.scale_value
        if visible >= info.height:
            self.page_scroll.set(0.0, 1.0)
            return
        top = max(0.0, min(self.vy, info.height - visible))
        self.page_scroll.set(top / info.height, (top + visible) / info.height)

    def _pan(self, dx_frac: float, dy_frac: float) -> None:
        vw, vh = self._viewport()
        self.vx += dx_frac * vw / self.scale_value
        self.vy += dy_frac * vh / self.scale_value
        self._clamp()
        self._schedule_render()

    # --------------------------------------------------------------- render

    def _schedule_render(self) -> None:
        self._update_vertical_scrollbar()
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_job = self.after(RENDER_DEBOUNCE_MS, self._render_now)

    def _render_now(self) -> None:
        self._render_job = None
        path = self.current_path
        info = self._info()
        if not path or not info:
            return
        vw, vh = self._viewport()
        self._token += 1
        self._worker.submit(_Request(self._token, path, self.vx, self.vy,
                                     self.scale_value, vw, vh, "mean"))
        self.view_var.set(self._view_text(info) + "   rendering…")

    def _view_text(self, info: pages.PageInfo) -> str:
        pct = self.scale_value * 100
        zoom = f"{pct:.0f}%" if pct >= 1 else f"1:{1 / self.scale_value:.0f}"
        return (f"zoom {zoom}   top-left ({int(self.vx)}, {int(self.vy)}) px   "
                f"row {max(0, int(self.vy))}/{info.height}")

    def _poll_results(self) -> None:
        latest = None
        while True:
            try:
                latest = self._results.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            request, image, error = latest
            if error is not None:
                self.status_var.set(f"render failed: {error}")
            elif request.token == self._token:
                self._draw(image)
        while True:
            try:
                kind, payload, error, manual = self._update_results.get_nowait()
            except queue.Empty:
                break
            if kind == "check":
                self._finish_update_check(payload, error, manual)
            elif kind == "download":
                self._finish_update_download(payload, error)
        self.after(40, self._poll_results)

    def _draw(self, image) -> None:
        self._photo = ImageTk.PhotoImage(image)
        if self._image_id is None:
            self._image_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        else:
            self.canvas.itemconfigure(self._image_id, image=self._photo)
            self.canvas.coords(self._image_id, 0, 0)
        info = self._info()
        if info:
            self.view_var.set(self._view_text(info))

    # -------------------------------------------------------------- verdicts

    def _ensure_log_type_option(self, value: str) -> str:
        existing = next((item for item in self.log_types
                         if item.casefold() == value.casefold()), None)
        if existing is None:
            existing = value
            self.log_types.append(existing)
        if existing not in self.log_type_vars:
            variable = tk.BooleanVar(value=False)
            self.log_type_vars[existing] = variable
            ttk.Checkbutton(
                self.log_type_frame,
                text=existing,
                variable=variable,
                command=self._on_draft_changed,
            ).pack(anchor="w", fill="x", pady=1)
        return existing

    def set_verdict(self, has_stamp: bool) -> None:
        """Set the stamp answer in the draft; submission is still explicit."""
        if self._baseline_draft is None:
            return
        self._draft_has_stamp = has_stamp
        if has_stamp:
            self.stamp_controls.grid()
            if not self._stamp_rows:
                self._add_stamp_row(focus=False)
        else:
            self.stamp_controls.grid_remove()
        self._refresh_verdict_buttons()
        self._refresh_submission_state()

    def _load_entry_draft(self, record) -> None:
        self._loading_draft = True
        try:
            draft = _EntryDraft.from_record(record)
            self._baseline_draft = draft
            self._entry_existed = record.has_entry
            self._draft_has_stamp = draft.has_stamp
            self.notes_var.set(draft.notes)

            for row in self._stamp_rows:
                row["frame"].destroy()
            self._stamp_rows.clear()
            for value in draft.stamp_types:
                self._add_stamp_row(value, focus=False)
            if draft.has_stamp is True and not self._stamp_rows:
                self._add_stamp_row(focus=False)

            for variable in self.log_type_vars.values():
                variable.set(False)
            for value in draft.log_types:
                canonical = self._ensure_log_type_option(value)
                self.log_type_vars[canonical].set(True)

            if draft.has_stamp is True:
                self.stamp_controls.grid()
            else:
                self.stamp_controls.grid_remove()
            self._refresh_verdict_buttons()
            self._update_stamp_choices()
        finally:
            self._loading_draft = False
        self._refresh_submission_state()

    def _refresh_verdict_buttons(self) -> None:
        selected = {"relief": "sunken", "bd": 3, "bg": "#cfe8d5"}
        ordinary = {"relief": "raised", "bd": 2, "bg": "SystemButtonFace"}
        self.yes_button.configure(**(selected if self._draft_has_stamp is True else ordinary))
        no_style = dict(selected)
        no_style["bg"] = "#ead4d1"
        self.no_button.configure(**(no_style if self._draft_has_stamp is False else ordinary))

    def _add_stamp_row(self, value: str = "", focus: bool = True) -> None:
        if self._draft_has_stamp is not True and not self._loading_draft:
            return
        frame = ttk.Frame(self.stamp_rows_frame)
        label = ttk.Label(frame, width=9, anchor="w")
        variable = tk.StringVar(value=value)
        combo = ttk.Combobox(frame, textvariable=variable, state="readonly", width=24)
        remove = ttk.Button(frame, text="Remove", width=8)
        row = {"frame": frame, "label": label, "var": variable,
               "combo": combo, "remove": remove}
        remove.configure(command=lambda item=row: self._remove_stamp_row(item))
        label.pack(side="left")
        combo.pack(side="left", fill="x", expand=True)
        remove.pack(side="left", padx=(5, 0))
        combo.bind("<<ComboboxSelected>>",
                   lambda _event, item=row: self._on_stamp_selected(item))
        self._stamp_rows.append(row)
        self._layout_stamp_rows()
        self._update_stamp_choices()
        if not self._loading_draft:
            self._refresh_submission_state()
        if focus:
            combo.focus_set()

    def _remove_stamp_row(self, row: dict[str, object]) -> None:
        if row not in self._stamp_rows or len(self._stamp_rows) <= 1:
            return
        row["frame"].destroy()
        self._stamp_rows.remove(row)
        self._layout_stamp_rows()
        self._update_stamp_choices()
        self._refresh_submission_state()

    def _layout_stamp_rows(self) -> None:
        for index, row in enumerate(self._stamp_rows):
            row["label"].configure(text=f"Stamp {index + 1}:")
            row["frame"].grid(row=index, column=0, sticky="ew", pady=(0, 3))
            row["remove"].configure(state="normal" if len(self._stamp_rows) > 1
                                    else "disabled")

    def _update_stamp_choices(self) -> None:
        selected = [str(row["var"].get()).strip() for row in self._stamp_rows]
        for row in self._stamp_rows:
            current = str(row["var"].get()).strip()
            unavailable = {value.casefold() for value in selected if value and value != current}
            choices = [value for value in self.types
                       if value.casefold() not in unavailable or value == current]
            row["combo"].configure(values=choices)
        complete_rows = bool(self._stamp_rows) and all(selected)
        unused_exists = len({value.casefold() for value in selected if value}) < len(self.types)
        self.add_stamp_button.configure(
            state="normal" if complete_rows and unused_exists else "disabled"
        )

    def _on_stamp_selected(self, _row=None) -> None:
        self._update_stamp_choices()
        self._refresh_submission_state()

    def _pick_type_by_number(self, n: int) -> None:
        if self._draft_has_stamp is not True or not (1 <= n <= len(self.types)):
            return
        target = next((row for row in self._stamp_rows
                       if self.focus_get() is row["combo"]), None)
        if target is None:
            target = next((row for row in self._stamp_rows
                           if not str(row["var"].get()).strip()), self._stamp_rows[0])
        value = self.types[n - 1]
        used_elsewhere = {
            str(row["var"].get()).strip().casefold()
            for row in self._stamp_rows if row is not target
        }
        if value.casefold() in used_elsewhere:
            self.entry_validation_var.set(f"{value} is already selected.")
            return
        target["var"].set(value)
        self._on_stamp_selected(target)

    def _add_type(self) -> None:
        name = simpledialog.askstring("Add a stamp type",
                                      "Name of the new stamp type:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if "," in name:
            messagebox.showerror(
                "Invalid stamp type",
                "Stamp type names cannot contain commas because commas separate "
                "multiple stamps in Excel.",
                parent=self,
            )
            return
        if name.lower() in {t.lower() for t in self.types}:
            name = next(t for t in self.types if t.lower() == name.lower())
        else:
            self.types.append(name)
            save_stamp_types(stamp_types_path(self.store.path), self.types)
        self._update_stamp_choices()
        if self._draft_has_stamp is True:
            target = next((row for row in self._stamp_rows
                           if not str(row["var"].get()).strip()), None)
            if target is None and len(self._stamp_rows) < len(self.types):
                self._add_stamp_row(name, focus=False)
            elif target is not None:
                target["var"].set(name)
                self._on_stamp_selected(target)

    def _on_draft_changed(self, *_args) -> None:
        if not self._loading_draft:
            self._refresh_submission_state()

    def _collect_draft(self) -> _EntryDraft:
        stamps = tuple(str(row["var"].get()).strip() for row in self._stamp_rows
                       if str(row["var"].get()).strip())
        if self._draft_has_stamp is not True:
            stamps = ()
        logs = tuple(value for value in self.log_types
                     if self.log_type_vars[value].get())
        return _EntryDraft(self._draft_has_stamp, stamps, logs,
                           self.notes_var.get().strip())

    def _draft_changed(self) -> bool:
        return self._baseline_draft is not None and self._collect_draft() != self._baseline_draft

    def _draft_valid(self) -> bool:
        if self._draft_has_stamp is not True:
            return True
        values = [str(row["var"].get()).strip() for row in self._stamp_rows]
        return bool(values) and all(values) and len({v.casefold() for v in values}) == len(values)

    def _refresh_submission_state(self) -> None:
        if self._baseline_draft is None:
            self.submit_button.configure(state="disabled", text="Add entry  →")
            return
        changed = self._draft_changed()
        valid = self._draft_valid()
        self.submit_button.configure(
            text="Update entry  →" if self._entry_existed else "Add entry  →",
            state="normal" if changed and valid else "disabled",
        )
        if self._draft_has_stamp is True and not valid:
            self.entry_validation_var.set("Select a different type for every stamp row.")
        elif changed:
            self.entry_validation_var.set("Ready to save this entry.")
            self.saved_var.set("DRAFT - click Add/Update entry")
        else:
            self.entry_validation_var.set("Make a change to add an entry.")
            if self.saved_var.get().startswith("DRAFT"):
                self.saved_var.set("entry unchanged")

    def submit_entry(self, advance: bool = True) -> bool:
        """Apply the current draft, save Excel, and optionally advance."""
        if self.store is None or self._baseline_draft is None or not self._draft_changed():
            return False
        if not self._draft_valid():
            messagebox.showwarning(
                "Incomplete stamp selection",
                "A Yes answer requires at least one stamp type, and each type can "
                "be selected only once.",
                parent=self,
            )
            return False

        record = self.current_record
        if record is None:
            return False
        draft = self._collect_draft()
        original = _EntryDraft.from_record(record)
        original_reviewed_at = record.reviewed_at
        original_reviewed_by = record.reviewed_by
        original_dirty = self.store.dirty
        if not self.reviewer:
            messagebox.showwarning("Reviewer required", "Select a reviewer before saving.",
                                   parent=self)
            return False
        record.has_stamp = draft.has_stamp
        record.stamp_type = join_values(draft.stamp_types) if draft.has_stamp is True else ""
        record.log_types = join_values(draft.log_types)
        record.notes = draft.notes
        record.reviewed_at = _dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")
        record.reviewed_by = self.reviewer
        self.store.dirty = True

        if not self._save_store(quiet=True):
            record.has_stamp = original.has_stamp
            record.stamp_type = join_values(original.stamp_types)
            record.log_types = join_values(original.log_types)
            record.notes = original.notes
            record.reviewed_at = original_reviewed_at
            record.reviewed_by = original_reviewed_by
            self.store.dirty = original_dirty
            self._refresh_submission_state()
            return False

        self._baseline_draft = draft
        self._entry_existed = record.has_entry
        self._refresh_row(self.index)
        self._refresh_submission_state()
        if advance:
            self.after(60, self.next_unreviewed)
        return True

    def _ask_draft_action(self) -> str:
        """Return ``save``, ``discard``, or ``cancel`` from an explicit dialog."""
        dialog = tk.Toplevel(self)
        dialog.title("Unsubmitted changes")
        dialog.transient(self)
        dialog.resizable(False, False)
        result = {"value": "cancel"}

        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="This log has changes that have not been added.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(body, text="What would you like to do before leaving this log?"
                  ).pack(anchor="w", pady=(5, 14))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def finish(value: str) -> None:
            result["value"] = value
            dialog.destroy()

        ttk.Button(buttons, text="Save entry", command=lambda: finish("save")
                   ).pack(side="left")
        ttk.Button(buttons, text="Discard changes", command=lambda: finish("discard")
                   ).pack(side="left", padx=(7, 0))
        ttk.Button(buttons, text="Cancel", command=lambda: finish("cancel")
                   ).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish("cancel"))
        dialog.bind("<Escape>", lambda _event: finish("cancel"))
        dialog.grab_set()
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        self.wait_window(dialog)
        return result["value"]

    def _resolve_pending_draft(self) -> bool:
        if not self._draft_changed():
            return True
        action = self._ask_draft_action()
        if action == "cancel":
            return False
        if action == "discard":
            record = self.current_record
            if record is not None:
                self._load_entry_draft(record)
            return True
        return self.submit_entry(advance=False)

    # ------------------------------------------------------------ navigation

    def step(self, delta: int) -> None:
        if self.paths:
            self.select_index(self.index + delta)

    def next_unreviewed(self) -> None:
        order = list(range(self.index + 1, len(self.paths))) + list(range(0, self.index + 1))
        for i in order:
            record = self.store.records.get(os.path.basename(self.paths[i]))
            if record is None or not record.reviewed:
                self.select_index(i)
                return
        scope = "subset" if self.view_mode == "subset" else "folder"
        self.status_var.set(f"every log in this {scope} has a verdict.")

    # ----------------------------------------------------------------- save

    def save(self, quiet: bool = False) -> bool:
        """Save the current draft without advancing, or flush committed rows."""
        if self._draft_changed():
            return self.submit_entry(advance=False)
        if self.store is None:
            return False
        if not self.store.dirty:
            if not quiet:
                self.status_var.set("All added entries are already saved.")
            return True
        return self._save_store(quiet=quiet)

    def _save_store(self, quiet: bool = False) -> bool:
        if self.store is None:
            return False
        order = [os.path.basename(p) for p in self.all_paths]
        try:
            path = self.store.save(order)
        except WorkbookLocked as exc:
            self.status_var.set(f"{exc} The draft is still available for retry.")
            messagebox.showerror("Workbook is open in Excel", str(exc), parent=self)
            return False
        except Exception as exc:
            messagebox.showerror("Could not save", f"{self.store.path}\n\n{exc}",
                                 parent=self)
            return False
        names = {os.path.basename(path).casefold() for path in self.all_paths}
        entries = sum(record.has_entry for name, record in self.store.records.items()
                      if name.casefold() in names)
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        self._refresh_progress()
        self.saved_var.set(f"saved {stamp} - {entries} entries")
        if not quiet:
            self.status_var.set(f"saved {entries} entries to {path}")
        return True

    # --------------------------------------------------------------- updates

    def _auto_check_for_updates(self) -> None:
        if update_check_due(self.cache_dir):
            self._check_for_updates(False)

    def _check_for_updates(self, manual: bool = False) -> None:
        if self._update_check_running:
            return
        if not _frozen():
            if manual:
                messagebox.showinfo(
                    "Updates",
                    "Automatic installation is available in the portable Windows app.\n\n"
                    f"This source checkout is LogReview {__version__}.",
                    parent=self,
                )
            return
        self._update_check_running = True
        self.update_button.configure(state="disabled", text="Checking...")

        def check() -> None:
            release = None
            error = None
            try:
                release = fetch_latest_release(__version__)
            except Exception as exc:
                error = exc
            finally:
                record_update_check(self.cache_dir)
            self._update_results.put(("check", release, error, manual))

        threading.Thread(target=check, daemon=True, name="logreview-update-check").start()

    def _finish_update_check(
        self, release: ReleaseInfo | None, error: Exception | None, manual: bool
    ) -> None:
        self._update_check_running = False
        self.update_button.configure(state="normal", text="Check for updates")
        if error is not None:
            if manual:
                messagebox.showerror(
                    "Could not check for updates",
                    f"LogReview {__version__} is still ready to use.\n\n{error}",
                    parent=self,
                )
            return
        if release is None:
            if manual:
                messagebox.showinfo(
                    "LogReview is up to date",
                    f"You are running the latest version: {__version__}.",
                    parent=self,
                )
            return
        self._offer_update(release)

    def _offer_update(self, release: ReleaseInfo) -> None:
        action = self._choice_dialog(
            "LogReview update available",
            f"LogReview {release.version} is available.\n\n"
            "Download and install it now? LogReview will close and restart. "
            "Review workbooks and image caches will be preserved.",
            [("Update now", "update"), ("Later", "later")],
            "later",
        )
        if action != "update":
            return
        if self.index >= 0 and not self._resolve_pending_draft():
            return
        self._start_update_download(release)

    def _start_update_download(self, release: ReleaseInfo) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Downloading LogReview update")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        body = ttk.Frame(dialog, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=f"Downloading and verifying LogReview {release.version}...",
        ).pack(anchor="w", pady=(0, 12))
        progress = ttk.Progressbar(body, mode="indeterminate", length=440)
        progress.pack(fill="x")
        progress.start(12)
        dialog.grab_set()
        self._update_progress = dialog

        def download() -> None:
            staged = None
            error = None
            try:
                staged = download_and_stage(release)
            except Exception as exc:
                error = exc
            self._update_results.put(("download", staged, error, True))

        threading.Thread(target=download, daemon=True, name="logreview-update-download").start()

    def _finish_update_download(
        self, staged: StagedUpdate | None, error: Exception | None
    ) -> None:
        if self._update_progress is not None:
            try:
                self._update_progress.grab_release()
                self._update_progress.destroy()
            except tk.TclError:
                pass
            self._update_progress = None
        if error is not None or staged is None:
            messagebox.showerror(
                "Update download failed",
                "Nothing in the installed application was changed.\n\n"
                + str(error or "The update could not be staged."),
                parent=self,
            )
            return
        try:
            launch_staged_update(staged)
        except Exception as exc:
            messagebox.showerror(
                "Could not start updater",
                f"Nothing in the installed application was changed.\n\n{exc}",
                parent=self,
            )
            return
        self._worker.shutdown()
        self.destroy()

    def _on_close(self) -> None:
        if not self._resolve_pending_draft():
            return
        if self.store is not None and self.store.dirty:
            answer = messagebox.askyesnocancel("Unsaved entries",
                                               "Save the review workbook before closing?")
            if answer is None:
                return
            if answer:
                self.save(quiet=True)
                if self.store.dirty:  # save failed; stay open
                    return
        self._worker.shutdown()
        self.destroy()

    # ----------------------------------------------------------------- keys

    def _on_key(self, event) -> None:
        widget = self.focus_get()
        key = event.keysym
        ctrl = bool(event.state & 0x0004)
        if ctrl and key in ("Return", "KP_Enter"):
            self.submit_entry()
            return
        if isinstance(widget, (ttk.Entry, tk.Entry, ttk.Combobox)):
            if event.keysym == "Escape":
                self.canvas.focus_set()
            return
        # Arrows belong to the list when the list has focus; they pan the viewer
        # only from the viewer. Y/N/zoom keys work from either.
        if widget is self.tree and key in ("Up", "Down", "Left", "Right",
                                          "Prior", "Next", "Home", "End"):
            return
        if ctrl:
            if key == "Right":
                self.step(1)
            elif key == "Left":
                self.step(-1)
            return
        actions = {
            "y": lambda: self.set_verdict(True),
            "n": lambda: self.set_verdict(False),
            "space": self.next_unreviewed,
            "t": self.goto_top,
            "b": self.goto_bottom,
            "w": self.fit_width,
            "f": self.fit_page,
            "plus": lambda: self._zoom_step(1),
            "equal": lambda: self._zoom_step(1),
            "kp_add": lambda: self._zoom_step(1),
            "minus": lambda: self._zoom_step(-1),
            "kp_subtract": lambda: self._zoom_step(-1),
            "up": lambda: self._pan(0, -0.25),
            "down": lambda: self._pan(0, 0.25),
            "left": lambda: self._pan(-0.25, 0),
            "right": lambda: self._pan(0.25, 0),
            "prior": lambda: self._pan(0, -0.9),
            "next": lambda: self._pan(0, 0.9),
            "escape": self.canvas.focus_set,
        }
        action = actions.get(key.lower())
        if action is not None:
            action()
            return
        if key in "123456789":
            self._pick_type_by_number(int(key))


# ------------------------------------------------------------------- helpers


def _frozen() -> bool:
    """True inside a PyInstaller build, where the source tree is not around."""
    return bool(getattr(sys, "frozen", False))


def _default_folder() -> str | None:
    # A reusable product cannot infer which corpus the user intends to review.
    return None


def _default_workbook(folder: str) -> str:
    """Where verdicts go by default. Never beside the images -- `data/raw/` is
    read-only, and a packaged copy may be pointed at a read-only share."""
    base = os.path.basename(os.path.normpath(folder)) or "logs"
    return os.path.join(_default_reviews_dir(), f"{base}_stamp_review.xlsx")


def _default_reviews_dir() -> str:
    """Default workbook directory beside the portable app or source launch."""
    root = os.path.dirname(sys.executable) if _frozen() else os.getcwd()
    return os.path.join(root, "reviews")


def _default_cache_dir() -> str:
    """Disposable pyramid cache, beside the portable app or launch directory."""
    root = os.path.dirname(sys.executable) if _frozen() else os.getcwd()
    return os.path.join(root, "cache")


def run(folder: str | None = None, workbook: str | None = None,
        cache_dir: str | None = None) -> int:
    app = ReviewApp(folder=folder, workbook=workbook, cache_dir=cache_dir)
    app.mainloop()
    return 0


def selftest(folder: str | None = None) -> int:
    """Drive the whole app once without a human, then exit. 0 = healthy.

    This is how a **packaged build** is verified: a frozen exe can fail on things
    a source run never sees (a missing GDAL DLL, no Tk runtime, openpyxl's
    templates left out of the bundle), and every one of those failures happens
    inside this routine -- open a real page, render it, record a verdict, write
    the workbook, read it back.
    """
    import tempfile
    import traceback

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")

    print("logcv review selftest")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            book = os.path.join(tmp, "selftest.xlsx")
            cache_dir = os.path.join(tmp, "cache")
            app = ReviewApp(folder=folder, workbook=book, cache_dir=cache_dir,
                            reviewer="SELFTEST")
            deadline = time.time() + 30
            while time.time() < deadline:       # let the first render land
                app.update()
                if app._photo is not None and not app.view_var.get().endswith("rendering…"):
                    break
                time.sleep(0.02)

            check("window built", app.winfo_exists() == 1)
            check(
                "product title",
                app.title().startswith("Geophysical Log Reviewer"),
                app.title(),
            )
            check("folder listed", bool(app.paths), f"{len(app.paths)} images")
            check("notes column visible", "notes" in app.tree["columns"])
            check("page opened", app.current_path is not None,
                  os.path.basename(app.current_path or ""))
            check("page rendered", app._photo is not None,
                  f"{app._photo.width()}x{app._photo.height()}" if app._photo else "no bitmap")
            check("reviewer displayed", app.reviewer_var.get() == "SELFTEST")
            check("update control available", app.update_button.winfo_exists() == 1)
            update_choices = []
            original_choice = app._choice_dialog
            try:
                def capture_update(_title, _message, choices, _cancel):
                    update_choices.extend(label for label, _value in choices)
                    return "later"

                app._choice_dialog = capture_update
                app._offer_update(ReleaseInfo(
                    version="9.9.9", tag="v9.9.9", asset_name="test.zip",
                    download_url="https://example.invalid/test.zip", size=1,
                    sha256="0" * 64,
                ))
            finally:
                app._choice_dialog = original_choice
            check(
                "update prompt has only Update now and Later",
                update_choices == ["Update now", "Later"],
                ", ".join(update_choices),
            )
            if _frozen():
                check(
                    "updater helper bundled",
                    os.path.isfile(os.path.join(os.path.dirname(sys.executable),
                                                "LogReviewUpdater.exe")),
                )
            check("bold control removed", not hasattr(app, "bold_var"))

            revealed = []
            original_popen = subprocess.Popen
            subprocess.Popen = lambda args, **_kwargs: revealed.append(args)
            try:
                app._reveal_current_tiff()
            finally:
                subprocess.Popen = original_popen
            check(
                "current TIFF reveal",
                bool(revealed) and os.path.normpath(app.current_path) in revealed[0][-1],
            )

            # Force a reduced-resolution view even on a narrow first page; the
            # ordinary opening fit may be factor 1 and therefore memory-only.
            prior_token = app._token
            app._set_scale(min(app.scale_value, 0.125), anchor=(0, 0))
            deadline = time.time() + 30
            while time.time() < deadline:
                app.update()
                if app._token > prior_token and not app.view_var.get().endswith("rendering…"):
                    break
                time.sleep(0.02)
            cached_tiles = []
            for dirpath, _, names in os.walk(cache_dir):
                cached_tiles.extend(os.path.join(dirpath, name)
                                    for name in names if name.endswith(".png"))
            check("pyramid cached", bool(cached_tiles), f"{len(cached_tiles)} tiles")

            # Exercise the primary navigation controls in the packaged build.
            app._on_vertical_scroll("moveto", "0.5")
            app.update()
            first_fraction, last_fraction = app.page_scroll.get()
            check(
                "vertical scrollbar",
                app.vy > 0 and first_fraction > 0 and last_fraction <= 1,
                f"thumb {first_fraction:.3f}-{last_fraction:.3f}",
            )

            before_zoom = app.scale_value
            event = type("WheelEvent", (), {"delta": 120, "x": 100, "y": 100})()
            result = app._on_wheel(event)
            app.update()
            check(
                "mouse wheel zoom",
                result == "break" and app.scale_value > before_zoom,
                f"{before_zoom:.5f} -> {app.scale_value:.5f}",
            )

            app.set_verdict(True)
            check("Yes requires a stamp type", str(app.submit_button["state"]) == "disabled")
            app._stamp_rows[0]["var"].set(app.types[0])
            app._on_stamp_selected(app._stamp_rows[0])
            app.log_type_vars["Gamma Ray"].set(True)
            app.log_type_vars["Spontaneous Potential"].set(True)
            app._on_draft_changed()
            check(
                "draft enables Add entry",
                str(app.submit_button["state"]) == "normal",
            )
            for _ in range(40):
                app.update()
                time.sleep(0.01)
            submitted = app.submit_entry(advance=False)
            check("entry submitted", submitted)

            # Notes/log classifications may be committed independently, but do
            # not complete the stamp-review stage.
            app.select_index(1)
            app.notes_var.set("selftest partial note")
            partial_submitted = app.submit_entry(advance=False)
            partial = app.current_record
            check(
                "note-only entry stays incomplete",
                partial_submitted and partial.incomplete and not partial.reviewed,
            )

            # A changed draft cannot be lost through list navigation.
            app.notes_var.set("unsubmitted replacement")
            original_ask = app._ask_draft_action
            app._ask_draft_action = lambda: "cancel"
            stayed = not app.select_index(2) and app.index == 1 and app._draft_changed()
            check("draft navigation cancel", stayed)
            app._ask_draft_action = lambda: "discard"
            discarded = app.select_index(2)
            check(
                "draft navigation discard",
                discarded and app.index == 2 and partial.notes == "selftest partial note",
            )
            app._ask_draft_action = original_ask

            # Repeatable visible rows serialize in their displayed order.
            if "TWDB" not in app.types:
                app.types.append("TWDB")
            app._update_stamp_choices()
            app.set_verdict(True)
            app._stamp_rows[0]["var"].set("IHS")
            app._on_stamp_selected(app._stamp_rows[0])
            app._add_stamp_row("TWDB", focus=False)
            multi_submitted = app.submit_entry(advance=False)
            check(
                "multiple stamps round-tripped in draft",
                multi_submitted and app.current_record.stamp_type == "IHS, TWDB",
                app.current_record.stamp_type,
            )

            # Simulate Excel's file lock: no record mutation and no draft loss.
            app.select_index(3)
            app.notes_var.set("must survive a failed save")
            original_save = app.store.save
            original_showerror = messagebox.showerror
            app.store.save = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                WorkbookLocked("simulated workbook lock")
            )
            messagebox.showerror = lambda *_args, **_kwargs: None
            try:
                failed_as_expected = not app.submit_entry(advance=False)
            finally:
                app.store.save = original_save
                messagebox.showerror = original_showerror
            check(
                "failed save retains draft",
                failed_as_expected
                and app.current_record.notes == ""
                and app.notes_var.get() == "must survive a failed save"
                and app._draft_changed(),
            )
            app._load_entry_draft(app.current_record)

            app.notes_var.set("saved while navigating")
            app._ask_draft_action = lambda: "save"
            saved_path = app.select_index(4)
            saved_record = app.store.records[os.path.basename(app.paths[3])]
            check(
                "draft navigation save",
                saved_path and app.index == 4 and saved_record.notes == "saved while navigating",
            )
            app._ask_draft_action = original_ask

            check("workbook written", os.path.exists(book),
                  f"{os.path.getsize(book) if os.path.exists(book) else 0} bytes")

            reloaded = ReviewStore(book)
            rows = reloaded.load()
            first = os.path.basename(app.paths[0]) if app.paths else ""
            verdict = reloaded.records.get(first)
            check("workbook read back", rows == len(app.paths), f"{rows} rows")
            check("verdict round-tripped", verdict is not None and verdict.has_stamp is True,
                  f"{first} -> {getattr(verdict, 'stamp_type', None)}")
            check(
                "log types round-tripped",
                verdict is not None
                and verdict.log_types == "Gamma Ray, Spontaneous Potential",
                getattr(verdict, "log_types", ""),
            )
            check(
                "reviewer round-tripped",
                verdict is not None and verdict.reviewed_by == "SELFTEST",
                getattr(verdict, "reviewed_by", ""),
            )

            leading_apis = []
            for path in app.all_paths:
                api = app._leading_api(path)
                if api and api not in leading_apis:
                    leading_apis.append(api)
            if leading_apis:
                app.store.replace_subset([leading_apis[0], "99999999999999"])
                subset_saved = app._save_store(quiet=True)
                app._refresh_scope_controls()
                subset_opened = app._switch_scope("subset")
                subset_count = len(app.paths)
                app._switch_scope("all")
                subset_reload = ReviewStore(book)
                subset_rows = subset_reload.load()
                check(
                    "subset sheet and scope preserve the full review",
                    subset_saved and subset_opened and subset_count >= 1
                    and subset_rows == len(app.all_paths)
                    and subset_reload.subset_apis
                    == [leading_apis[0], "99999999999999"],
                    f"{subset_count} subset / {subset_rows} workbook rows",
                )
            else:
                check("subset sheet and scope preserve the full review", False,
                      "no leading API in self-test TIFF names")
            try:
                validate_review_workbook(
                    book, {os.path.basename(path) for path in app.all_paths}
                )
                workbook_valid = True
            except InvalidWorkbook:
                workbook_valid = False
            check("workbook validates for resume", workbook_valid)

            # Cancelling either file browser returns to the workbook choices.
            original_action = app._ask_workbook_action
            original_openfilename = filedialog.askopenfilename
            original_savefilename = filedialog.asksaveasfilename
            workbook_browser_dirs = []
            try:
                actions = iter(("existing", "back"))
                app._ask_workbook_action = lambda: next(actions)
                filedialog.askopenfilename = lambda **kwargs: (
                    workbook_browser_dirs.append(kwargs.get("initialdir")) or ""
                )
                existing_cancelled = not app._choose_workbook(app.folder, app.paths)

                actions = iter(("new", "back"))
                app._ask_workbook_action = lambda: next(actions)
                filedialog.asksaveasfilename = lambda **kwargs: (
                    workbook_browser_dirs.append(kwargs.get("initialdir")) or ""
                )
                new_cancelled = not app._choose_workbook(app.folder, app.paths)
            finally:
                app._ask_workbook_action = original_action
                filedialog.askopenfilename = original_openfilename
                filedialog.asksaveasfilename = original_savefilename
            check("existing workbook cancel returns to choices", existing_cancelled)
            check("new workbook cancel returns to choices", new_cancelled)
            expected_reviews = _default_reviews_dir()
            check(
                "workbook browsers default to reviews folder",
                workbook_browser_dirs == [expected_reviews, expected_reviews]
                and os.path.isdir(expected_reviews),
                expected_reviews,
            )

            original_askstring = simpledialog.askstring
            simpledialog.askstring = lambda *_args, **_kwargs: None
            try:
                reviewer_cancelled = app._prompt_reviewer("SELFTEST") is None
            finally:
                simpledialog.askstring = original_askstring
            check("reviewer cancel is recoverable", reviewer_cancelled)

            # Exercise destructive and non-destructive reconciliation in temp.
            reconcile_book = os.path.join(tmp, "reconcile.xlsx")
            reconcile_store = ReviewStore(reconcile_book)
            reconcile_store.record_for(os.path.join(tmp, "current.tif")).set_verdict(False)
            reconcile_store.record_for(os.path.join(tmp, "extra.tif")).set_verdict(
                True, "Historic"
            )
            reconcile_store.save(["current.tif", "extra.tif"])
            inspection = validate_review_workbook(reconcile_book, {"current.tif"})
            original_showinfo = messagebox.showinfo
            original_showerror = messagebox.showerror
            messagebox.showinfo = lambda *_args, **_kwargs: None
            messagebox.showerror = lambda *_args, **_kwargs: None
            try:
                removed_ok = app._remove_extra_rows(reconcile_book, inspection)
            finally:
                messagebox.showinfo = original_showinfo
                messagebox.showerror = original_showerror
            backups = [name for name in os.listdir(tmp)
                       if name.startswith("reconcile.pre_reconcile_")]
            reconciled = validate_review_workbook(reconcile_book, {"current.tif"})
            backup_store = ReviewStore(os.path.join(tmp, backups[0])) if backups else None
            backup_rows = backup_store.load() if backup_store else 0
            check(
                "extra-row removal creates backup",
                removed_ok and bool(backups) and backup_rows == 2
                and reconciled.extra_names == (),
            )

            original_choice = app._choice_dialog
            try:
                app._choice_dialog = lambda *_args, **_kwargs: "back"
                back_result = not app._handle_reconciliation(reconcile_book, WorkbookInspection(
                    ("current.tif",), ("missing.tif",), ()
                ))
                app._choice_dialog = lambda *_args, **_kwargs: "keep"
                keep_result = app._handle_reconciliation(reconcile_book, WorkbookInspection(
                    ("current.tif", "extra.tif"), (), ("extra.tif",)
                ))
            finally:
                app._choice_dialog = original_choice
            check("reconciliation Back is non-destructive", back_result)
            check("reconciliation Keep continues", keep_result)

            app._worker.shutdown()
            app.destroy()
    except Exception:
        traceback.print_exc()
        check("no exception", False)

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed"
          + (f" - FAILED: {', '.join(failed)}" if failed else " - OK"))
    return 1 if failed else 0
