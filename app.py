"""Duplicate Finder - the window.

All the real work happens in finder.py. This file only shows things and
reacts to clicks. Scanning runs in a background thread so the window
stays responsive on big folders.

Run with:  python app.py
"""

import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from finder import (
    DuplicateGroup,
    find_duplicates,
    human_size,
    list_files,
    plan_removals,
    protected_reason,
    remove_copies,
)

COMMON_FOLDERS = ["Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music"]


def app_data_dir() -> Path:
    """Where the logs live: %LOCALAPPDATA%\\DuplicateFinder."""
    base = os.environ.get("LOCALAPPDATA")
    folder = Path(base) / "DuplicateFinder" if base else Path.home() / ".duplicate-finder"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


DATA_DIR = app_data_dir()
REMOVED_LOG = DATA_DIR / "removed.log"

# Full error details go to this file; the user only sees friendly messages.
logging.basicConfig(
    filename=DATA_DIR / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def short(path: Path) -> str:
    """Show paths relative to the user folder: Pictures\\photo.jpg."""
    try:
        return str(path.relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def modified(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


class FinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Duplicate Finder")
        self.geometry("900x620")
        self.minsize(660, 480)

        self.groups: list[DuplicateGroup] = []
        self.row_lookup: dict[str, tuple[int, Path]] = {}  # tree row id -> (group index, file)
        self.folder_vars: dict[Path, tk.BooleanVar] = {}
        self.scan_output = None  # filled in by the background thread

        self.build_folder_section()
        self.build_buttons()
        self.build_results()
        self.refresh_buttons()

    # ---------------------------------------------------------------- layout

    def build_folder_section(self):
        box = ttk.LabelFrame(self, text="1. Choose folders to scan (subfolders included)", padding=10)
        box.pack(fill="x", padx=12, pady=(12, 6))

        self.folder_list = ttk.Frame(box)
        self.folder_list.pack(fill="x")
        for name in COMMON_FOLDERS:
            folder = Path.home() / name
            if folder.is_dir():
                self.add_folder_checkbox(folder, checked=False)

        ttk.Button(box, text="Add another folder…", command=self.on_add_folder).pack(anchor="w", pady=(8, 0))

    def add_folder_checkbox(self, folder: Path, checked: bool):
        var = tk.BooleanVar(value=checked)
        self.folder_vars[folder] = var
        index = len(self.folder_vars) - 1
        ttk.Checkbutton(self.folder_list, text=short(folder), variable=var).grid(
            row=index // 3, column=index % 3, sticky="w", padx=(0, 24), pady=2
        )

    def build_buttons(self):
        row = ttk.Frame(self, padding=(12, 0))
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="2.").pack(side="left")
        self.scan_button = ttk.Button(row, text="Scan", command=self.on_scan)
        self.scan_button.pack(side="left", padx=(4, 8))
        ttk.Label(row, text="3.").pack(side="left")
        self.remove_button = ttk.Button(row, text="Remove extra copies", command=self.on_remove)
        self.remove_button.pack(side="left", padx=(4, 8))
        self.show_button = ttk.Button(row, text="Show in folder", command=self.on_show_in_folder)
        self.show_button.pack(side="right")
        self.keep_button = ttk.Button(row, text="Keep this copy", command=self.on_keep_selected)
        self.keep_button.pack(side="right", padx=(0, 8))

    def build_results(self):
        box = ttk.LabelFrame(
            self, text="Duplicates — double-click a copy to keep it instead. Nothing is removed until step 3.",
            padding=8,
        )
        box.pack(fill="both", expand=True, padx=12, pady=(6, 4))

        self.tree = ttk.Treeview(box, columns=("action", "modified"), show="tree headings")
        self.tree.heading("#0", text="File")
        self.tree.heading("action", text="Action")
        self.tree.heading("modified", text="Modified")
        self.tree.column("#0", width=560)
        self.tree.column("action", width=110, stretch=False)
        self.tree.column("modified", width=140, stretch=False)
        self.tree.tag_configure("group", font=("TkDefaultFont", 9, "bold"))
        self.tree.bind("<Double-1>", lambda event: self.on_keep_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.refresh_buttons())

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.status = ttk.Label(self, text="Tick some folders, then click Scan.", padding=(12, 0, 12, 10))
        self.status.pack(fill="x")

    def refresh_buttons(self, scanning: bool = False):
        def enable(button, on):
            button.state(["!disabled"] if on else ["disabled"])

        selected_file = self.selected_row() is not None
        enable(self.scan_button, not scanning)
        enable(self.remove_button, bool(self.groups) and not scanning)
        enable(self.keep_button, selected_file and not scanning)
        enable(self.show_button, selected_file)

    # ------------------------------------------------------------- scanning

    def selected_folders(self) -> list[Path]:
        return [folder for folder, var in self.folder_vars.items() if var.get()]

    def on_add_folder(self):
        chosen = filedialog.askdirectory(title="Choose a folder to scan")
        if not chosen:
            return
        folder = Path(chosen)
        reason = protected_reason(folder)
        if reason:
            messagebox.showwarning("Can't scan that folder", f"That folder can't be scanned because {reason}.")
            return
        if folder in self.folder_vars:
            self.folder_vars[folder].set(True)
            return
        self.add_folder_checkbox(folder, checked=True)

    def on_scan(self):
        folders = self.selected_folders()
        if not folders:
            messagebox.showinfo("No folders chosen", "Tick at least one folder first.")
            return

        self.groups = []
        self.show_groups()
        self.status.config(text="Scanning… big folders can take a few minutes.")
        self.refresh_buttons(scanning=True)
        self.scan_output = None
        threading.Thread(target=self.scan_in_background, args=(folders,), daemon=True).start()
        self.after(200, self.check_scan_finished)

    def scan_in_background(self, folders: list[Path]):
        """Runs on a background thread: no window code allowed in here."""
        try:
            scan = list_files(folders)
            groups, problems = find_duplicates(scan.files)
            self.scan_output = ("ok", groups, scan.problems + problems)
        except Exception:
            logging.exception("Scan failed")
            self.scan_output = ("error", [], [])

    def check_scan_finished(self):
        """Runs on the window's thread, every 200 ms until the scan is done."""
        if self.scan_output is None:
            self.after(200, self.check_scan_finished)
            return

        outcome, groups, problems = self.scan_output
        self.refresh_buttons()
        if outcome == "error":
            self.status.config(text="The scan stopped unexpectedly.")
            messagebox.showerror(
                "Something went wrong",
                f"Sorry, the scan stopped unexpectedly. Nothing was changed.\n\nDetails: {DATA_DIR / 'app.log'}",
            )
            return

        self.groups = groups
        self.show_groups()
        self.refresh_buttons()
        if problems:
            self.show_problems("Some items couldn't be checked", problems)

    # -------------------------------------------------------------- results

    def show_groups(self):
        self.tree.delete(*self.tree.get_children())
        self.row_lookup.clear()

        for g_index, group in enumerate(self.groups):
            group_id = f"g{g_index}"
            title = (
                f"{len(group.files)} copies · {human_size(group.size)} each · "
                f"{human_size(group.wasted)} can be freed"
            )
            self.tree.insert("", "end", iid=group_id, text=title, open=True, tags=("group",))
            for f_index, path in enumerate(group.files):
                row_id = f"{group_id}:{f_index}"
                action = "Keep" if path == group.keeper else "Remove"
                self.tree.insert(group_id, "end", iid=row_id, text=short(path), values=(action, modified(path)))
                self.row_lookup[row_id] = (g_index, path)

        if self.groups:
            total = sum(g.wasted for g in self.groups)
            self.status.config(
                text=f"Found {len(self.groups)} group(s) of duplicates. {human_size(total)} can be freed. "
                "Check the Keep/Remove column, then click Remove extra copies."
            )
        elif self.status.cget("text").startswith("Scanning"):
            self.status.config(text="No duplicates found in those folders.")

    def selected_row(self) -> tuple[int, Path] | None:
        selection = self.tree.selection()
        return self.row_lookup.get(selection[0]) if selection else None

    def on_keep_selected(self):
        row = self.selected_row()
        if row is None:
            return
        g_index, path = row
        self.groups[g_index].set_keeper(path)
        self.show_groups()
        self.tree.selection_set(f"g{g_index}:0")  # the kept copy is now first
        self.tree.see(f"g{g_index}:0")

    def on_show_in_folder(self):
        row = self.selected_row()
        if row is None:
            return
        path = row[1]
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", str(path)])
        else:
            messagebox.showinfo("File location", str(path))

    # ------------------------------------------------------------- removing

    def on_remove(self):
        removals = plan_removals(self.groups)
        if not removals:
            return
        total = sum(r.size for r in removals)
        if not messagebox.askyesno(
            "Remove extra copies?",
            f"Send {len(removals)} extra copies ({human_size(total)}) to the Recycle Bin?\n\n"
            "One copy of every file is kept. You can restore from the Recycle Bin if needed.",
        ):
            return

        self.config(cursor="watch")
        self.update_idletasks()
        try:
            result = remove_copies(removals, REMOVED_LOG)
        finally:
            self.config(cursor="")

        self.groups = []
        self.show_groups()
        self.refresh_buttons()
        self.status.config(text=f"Done: {result.done} copies removed, {human_size(result.freed)} freed.")

        message = (
            f"{result.done} copies sent to the Recycle Bin, {human_size(result.freed)} freed.\n\n"
            f"A list of what was removed is in:\n{REMOVED_LOG}"
        )
        if result.failed:
            self.show_problems("Removed, with some skipped", result.failed, intro=message + "\n\nSkipped:")
        else:
            messagebox.showinfo("Removed", message)

    # --------------------------------------------------------------- helpers

    def show_problems(self, title: str, problems: list[str], intro: str = ""):
        shown = "\n".join(f"• {p}" for p in problems[:10])
        more = f"\n…and {len(problems) - 10} more" if len(problems) > 10 else ""
        messagebox.showwarning(title, f"{intro}\n{shown}{more}".strip())

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        """Any unexpected error: full details to app.log, a friendly box for the user."""
        logging.error("Unexpected error", exc_info=(exc_type, exc_value, exc_traceback))
        self.config(cursor="")
        messagebox.showerror(
            "Something went wrong",
            f"Sorry, something unexpected happened.\n\nDetails were saved to:\n{DATA_DIR / 'app.log'}",
        )


def enable_sharp_text_on_windows():
    """Stop Windows from blurring the window on scaled (e.g. 125%) displays."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass  # not Windows, or an older Windows: the window still works


if __name__ == "__main__":
    enable_sharp_text_on_windows()
    FinderApp().mainloop()