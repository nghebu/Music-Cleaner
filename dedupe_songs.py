#!/usr/bin/env python3
"""
Song De-duplicator  (companion to YT-M Downloader)

Scans a folder of audio files and finds duplicates using several methods,
then lets you report / move / delete them. Similar songs are grouped together.

Detection tiers (each can be toggled):
  - Exact      : same YouTube video id (from "[id]" in the name) OR byte-identical
                 audio (SHA-256). Always on. No false positives.
  - Names      : fuzzy match on cleaned-up title + artist (rapidfuzz). Catches
                 "Song" vs "Song (Official Video)".
  - Audio      : acoustic fingerprint of the actual sound (Chromaprint / fpcalc).
                 Catches the same recording even with totally different names.

Actions:
  - Report only        : touch nothing, write dedupe_report.csv + .txt
  - Move to _duplicates: move the extras into a _duplicates subfolder (safe)
  - Delete extras      : keep one per group, delete the rest (asks first)

"Keep" rule per group: the largest file (best quality); ties -> shortest name.

Needs: Python 3.8+. Optional: mutagen, rapidfuzz (names), fpcalc (audio).
Run setup_dedupe.bat once to install them.
"""

import os
import re
import csv
import sys
import queue
import shutil
import hashlib
import threading
import subprocess
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(SCRIPT_DIR, "downloads")
AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".webm", ".flac", ".ogg", ".wav")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
PAREN_RE = re.compile(r"[\(\[\{].*?[\)\]\}]")
NOISE_RE = re.compile(
    r"\b(official|video|audio|lyrics?|lyric|mv|m/v|hd|hq|4k|live|remaster(ed)?|"
    r"visualizer|performance|color\s*coded|full\s*album|feat\.?|ft\.?)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Helpers (pure functions, unit-testable)
# ---------------------------------------------------------------------------
def video_id_from_name(name):
    m = ID_RE.search(name)
    return m.group(1) if m else ""


def normalize_title(text):
    """Lowercase, drop bracketed bits / noise words / punctuation, collapse spaces."""
    if not text:
        return ""
    s = text.lower()
    s = ID_RE.sub(" ", s)
    s = PAREN_RE.sub(" ", s)
    s = NOISE_RE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')      # illegal in Windows filenames
LEAD_NUM = re.compile(r"^\s*\d+\s*[-_.)\]]*\s*")     # leading "001 - ", "01", "5)" etc.


def sanitize_filename(s):
    """Strip characters Windows won't allow in a filename."""
    s = WIN_BAD.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".").strip()
    return s or "untitled"


def clean_base_from_track(track, fmt):
    """Desired filename (no extension) from tags. Falls back to a cleaned filename.
    fmt = 'artist_title' -> 'Artist - Title';  'title' -> 'Title'."""
    title = (track.title or "").strip()
    artist = (track.artist or "").strip()
    if not title:
        # no usable tag -> clean the existing filename: drop [id] and leading number
        base = os.path.splitext(track.name)[0]
        base = ID_RE.sub("", base)
        base = LEAD_NUM.sub("", base)
        title = base.strip() or os.path.splitext(track.name)[0]
    if fmt == "artist_title" and artist:
        name = f"{artist} - {title}"
    else:
        name = title
    return sanitize_filename(name)


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def popcount(x):
    try:
        return x.bit_count()        # Python 3.10+
    except AttributeError:
        return bin(x).count("1")


def fp_similarity(a, b):
    """0..1 similarity between two raw Chromaprint int vectors."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    diff = 0
    for i in range(n):
        diff += popcount((a[i] ^ b[i]) & 0xFFFFFFFF)
    return 1.0 - diff / (32.0 * n)


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self):
        out = {}
        for x in self.parent:
            out.setdefault(self.find(x), []).append(x)
        return list(out.values())


def find_fpcalc():
    p = shutil.which("fpcalc")
    if p:
        return p
    local = os.path.join(SCRIPT_DIR, "fpcalc.exe")
    return local if os.path.isfile(local) else None


def run_fpcalc(fpcalc, path, length=120):
    """Return (duration_seconds, [int,...]) using Chromaprint raw fingerprint."""
    out = subprocess.run(
        [fpcalc, "-raw", "-length", str(length), path],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    ).stdout
    dur, fp = 0.0, []
    for line in out.splitlines():
        if line.startswith("DURATION="):
            try:
                dur = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("FINGERPRINT="):
            fp = [int(x) for x in line.split("=", 1)[1].split(",") if x]
    return dur, fp


# ---------------------------------------------------------------------------
# A scanned track
# ---------------------------------------------------------------------------
class Track:
    __slots__ = ("path", "name", "size", "vid", "title", "artist", "norm",
                 "sha", "dur", "fp")

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        self.vid = video_id_from_name(self.name)
        self.title = ""
        self.artist = ""
        self.norm = ""
        self.sha = ""
        self.dur = 0.0
        self.fp = []

    def read_tags(self):
        title = artist = ""
        try:
            from mutagen import File as MFile
            mf = MFile(self.path, easy=True)
            if mf is not None:
                title = (mf.get("title") or [""])[0]
                artist = (mf.get("artist") or [""])[0]
                if getattr(mf, "info", None) is not None:
                    self.dur = float(getattr(mf.info, "length", 0) or 0)
        except Exception:
            pass
        if not title:
            title = os.path.splitext(self.name)[0]
        self.title, self.artist = title, artist
        self.norm = normalize_title(f"{artist} {title}").strip() or normalize_title(self.name)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Song De-duplicator")
        root.geometry("860x620")
        root.minsize(720, 520)

        self.log_q = queue.Queue()
        self.groups = []          # list of dicts: {keep: Track, dups: [Track], reason: str}
        self.busy = False
        self._cancel = False
        self.fpcalc = find_fpcalc()

        self._build_ui()
        self._poll_log()
        self._startup_notes()

    # --- UI ----------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Folder:").grid(row=0, column=0, sticky="w")
        self.dir_var = tk.StringVar(value=DEFAULT_DIR)
        ttk.Entry(top, textvariable=self.dir_var, width=72).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._browse).grid(row=0, column=4)
        top.columnconfigure(1, weight=1)

        opt = ttk.LabelFrame(self.root, text="Detect duplicates by")
        opt.pack(fill="x", **pad)

        self.exact_var = tk.BooleanVar(value=True)
        c = ttk.Checkbutton(opt, text="Exact (video id / identical audio)",
                            variable=self.exact_var, state="disabled")
        c.grid(row=0, column=0, sticky="w", padx=6, pady=2)

        self.name_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Similar names", variable=self.name_var).grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="match %").grid(row=0, column=2, sticky="e")
        self.name_thr = tk.IntVar(value=90)
        ttk.Spinbox(opt, from_=70, to=100, width=5, textvariable=self.name_thr).grid(
            row=0, column=3, sticky="w")

        self.audio_var = tk.BooleanVar(value=bool(self.fpcalc))
        self.audio_chk = ttk.Checkbutton(
            opt, text="Audio fingerprint (sound itself)", variable=self.audio_var)
        self.audio_chk.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        if not self.fpcalc:
            self.audio_chk.config(state="disabled")
        ttk.Label(opt, text="match %").grid(row=1, column=2, sticky="e")
        self.audio_thr = tk.IntVar(value=85)
        ttk.Spinbox(opt, from_=70, to=100, width=5, textvariable=self.audio_thr).grid(
            row=1, column=3, sticky="w")

        act = ttk.LabelFrame(self.root, text="When I click Apply")
        act.pack(fill="x", **pad)
        self.action_var = tk.StringVar(value="report")
        for i, (val, txt) in enumerate([
            ("report", "Report only (write a list, change nothing)"),
            ("move", "Move extras to a _duplicates subfolder"),
            ("delete", "Delete extras (keep one per group)"),
        ]):
            ttk.Radiobutton(act, text=txt, value=val, variable=self.action_var).grid(
                row=i, column=0, sticky="w", padx=6, pady=1)

        ren = ttk.LabelFrame(self.root, text="Clean filenames (strip 001/track numbers + [id])")
        ren.pack(fill="x", **pad)
        self.ren_fmt = tk.StringVar(value="artist_title")
        ttk.Radiobutton(ren, text="Artist - Title", value="artist_title",
                        variable=self.ren_fmt).grid(row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Radiobutton(ren, text="Title only", value="title",
                        variable=self.ren_fmt).grid(row=0, column=1, sticky="w", padx=6)
        self.rename_btn = ttk.Button(ren, text="Preview + Rename", command=self.rename_clean)
        self.rename_btn.grid(row=0, column=2, padx=12)
        ttk.Label(ren, text="(uses the song's tags; previews first, asks before changing anything)"
                  ).grid(row=1, column=0, columnspan=3, sticky="w", padx=6)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(btns, text="1. Scan", command=self.scan)
        self.scan_btn.pack(side="left", padx=4)
        self.apply_btn = ttk.Button(btns, text="2. Apply", command=self.apply,
                                    state="disabled")
        self.apply_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Open folder", command=self._open).pack(side="right", padx=4)

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=12)

        logfrm = ttk.Frame(self.root)
        logfrm.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = tk.Text(logfrm, wrap="word", bg="#111", fg="#ddd",
                           insertbackground="#ddd")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logfrm, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or SCRIPT_DIR)
        if d:
            self.dir_var.set(d)

    def _open(self):
        d = self.dir_var.get()
        if os.path.isdir(d):
            try:
                os.startfile(d)
            except AttributeError:
                subprocess.Popen(["xdg-open", d])

    # --- logging -----------------------------------------------------------
    def logln(self, t=""):
        self.log_q.put(t + "\n")

    def _poll_log(self):
        try:
            while True:
                self.log.insert("end", self.log_q.get_nowait())
                self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def _startup_notes(self):
        try:
            import rapidfuzz  # noqa
        except ImportError:
            self.logln("[!] rapidfuzz not installed -> 'Similar names' is limited to "
                       "exact name matches. Run setup_dedupe.bat.")
        try:
            import mutagen  # noqa
        except ImportError:
            self.logln("[!] mutagen not installed -> names come from filenames only. "
                       "Run setup_dedupe.bat.")
        if self.fpcalc:
            self.logln(f"[ok] fpcalc found: {self.fpcalc}")
        else:
            self.logln("[!] fpcalc not found -> audio fingerprint disabled. "
                       "Run setup_dedupe.bat to download it.")
        self.logln("")

    def _set_busy(self, b):
        self.busy = b
        st = "disabled" if b else "normal"
        self.scan_btn.config(state=st)
        self.rename_btn.config(state=st)
        self.apply_btn.config(state="normal" if (not b and self.groups) else "disabled")
        self.cancel_btn.config(state="normal" if b else "disabled")

    def cancel(self):
        self._cancel = True
        self.status.set("Cancelling...")

    # --- rename (clean filenames) -----------------------------------------
    def rename_clean(self):
        folder = self.dir_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("Dedupe", "Pick a real folder first.")
            return
        if self.busy:
            return
        self._set_busy(True)
        self.status.set("Planning rename...")
        self.logln(f"\n=== Rename preview @ {datetime.now():%H:%M:%S} ===")
        threading.Thread(target=self._rename_worker,
                         args=(folder, self.ren_fmt.get()), daemon=True).start()

    def _rename_worker(self, folder, fmt):
        try:
            files = sorted(f for f in os.listdir(folder)
                           if f.lower().endswith(AUDIO_EXTS)
                           and os.path.isfile(os.path.join(folder, f)))
            if not files:
                self.logln("No audio files to rename here.")
                return
            plan = []          # (old_name, new_name)
            used = set()       # final names (lowercased) to avoid collisions
            for f in files:
                t = Track(os.path.join(folder, f))
                t.read_tags()
                ext = os.path.splitext(f)[1].lower()
                base = clean_base_from_track(t, fmt)
                newname = base + ext
                key = newname.lower()
                k = 2
                while key in used or (
                        os.path.normcase(newname) != os.path.normcase(f)
                        and os.path.exists(os.path.join(folder, newname))):
                    newname = f"{base} ({k}){ext}"
                    key = newname.lower()
                    k += 1
                used.add(key)
                if newname != f:
                    plan.append((f, newname))
            self.rename_plan = (folder, plan)
            for old, new in plan[:120]:
                self.logln(f"  {old}\n      ->  {new}")
            if len(plan) > 120:
                self.logln(f"  ... and {len(plan) - 120} more")
            if not plan:
                self.logln("Everything already looks clean - nothing to rename.")
                self.status.set("Nothing to rename.")
                return
            self.logln(f"\n{len(plan)} of {len(files)} files would be renamed.")
            self.status.set(f"{len(plan)} files to rename - confirm to apply.")
            self.root.after(0, self._confirm_rename)
        except Exception as e:
            self.logln(f"[error] {e}")
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def _confirm_rename(self):
        folder, plan = getattr(self, "rename_plan", (None, []))
        if not plan:
            return
        if messagebox.askyesno(
                "Rename", f"Rename {len(plan)} files to clean names?\n\n"
                          "This changes filenames only - the audio is untouched."):
            self._set_busy(True)
            self.status.set("Renaming...")
            threading.Thread(target=self._apply_rename,
                             args=(folder, plan), daemon=True).start()

    def _apply_rename(self, folder, plan):
        done = fail = 0
        self.logln(f"\n=== Renaming @ {datetime.now():%H:%M:%S} ===")
        for old, new in plan:
            op = os.path.join(folder, old)
            np = os.path.join(folder, new)
            try:
                if (os.path.exists(np)
                        and os.path.normcase(op) != os.path.normcase(np)):
                    b, ext = os.path.splitext(new)
                    k = 2
                    while os.path.exists(os.path.join(folder, f"{b} ({k}){ext}")):
                        k += 1
                    np = os.path.join(folder, f"{b} ({k}){ext}")
                os.rename(op, np)
                done += 1
            except Exception as e:
                fail += 1
                self.logln(f"  [fail] {old}: {e}")
        self.logln(f"Renamed {done} files." + (f" {fail} failed." if fail else ""))
        self.status.set(f"Renamed {done} files.")
        self.root.after(0, lambda: self._set_busy(False))

    # --- scan --------------------------------------------------------------
    def scan(self):
        folder = self.dir_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("Dedupe", "Pick a real folder first.")
            return
        self.groups = []
        self._cancel = False
        self._set_busy(True)
        self.status.set("Scanning...")
        self.logln(f"=== Scan @ {datetime.now():%H:%M:%S} ===")
        self.logln(f"Folder: {folder}")
        threading.Thread(target=self._scan_worker, args=(folder,), daemon=True).start()

    def _scan_worker(self, folder):
        try:
            files = [os.path.join(folder, f) for f in os.listdir(folder)
                     if f.lower().endswith(AUDIO_EXTS)
                     and os.path.isfile(os.path.join(folder, f))]
            files.sort()
            if not files:
                self.logln("No audio files found here.")
                return self._done_scan()

            self.logln(f"Found {len(files)} audio files. Reading info...")
            tracks = []
            for i, p in enumerate(files, 1):
                if self._cancel:
                    return self._done_scan()
                t = Track(p)
                t.read_tags()
                tracks.append(t)
                if i % 100 == 0:
                    self.logln(f"  read {i}/{len(files)}")

            uf = UnionFind([t.path for t in tracks])
            reasons = {}

            def link(a, b, why):
                uf.union(a.path, b.path)
                reasons[frozenset((a.path, b.path))] = why

            # Tier 1: exact -- video id
            by_id = {}
            for t in tracks:
                if t.vid:
                    by_id.setdefault(t.vid, []).append(t)
            for vid, grp in by_id.items():
                for t in grp[1:]:
                    link(grp[0], t, f"same video id {vid}")

            # Tier 1: exact -- identical bytes (hash only same-size files)
            self.logln("Hashing for byte-identical copies...")
            by_size = {}
            for t in tracks:
                by_size.setdefault(t.size, []).append(t)
            for size, grp in by_size.items():
                if len(grp) < 2:
                    continue
                for t in grp:
                    if self._cancel:
                        return self._done_scan()
                    if not t.sha:
                        t.sha = sha256(t.path)
                by_hash = {}
                for t in grp:
                    by_hash.setdefault(t.sha, []).append(t)
                for h, g2 in by_hash.items():
                    for t in g2[1:]:
                        link(g2[0], t, "identical audio file")

            # Tier 2: fuzzy names
            if self.name_var.get():
                self.logln("Comparing names...")
                self._match_names(tracks, link)

            # Tier 3: audio fingerprint
            if self.audio_var.get() and self.fpcalc:
                self.logln("Fingerprinting audio (this is the slow part)...")
                self._match_audio(tracks, link)

            # Build groups
            tmap = {t.path: t for t in tracks}
            raw_groups = [g for g in uf.groups() if len(g) > 1]
            self.groups = []
            for g in raw_groups:
                members = sorted((tmap[p] for p in g),
                                 key=lambda t: (-t.size, len(t.name), t.name))
                keep = members[0]
                dups = members[1:]
                why = set()
                for d in dups:
                    why.add(reasons.get(frozenset((keep.path, d.path)), "similar"))
                self.groups.append({"keep": keep, "dups": dups,
                                    "reason": ", ".join(sorted(why)) or "similar"})

            # sort groups so similar names sit together
            self.groups.sort(key=lambda gr: gr["keep"].norm)
            self._report(folder, tracks)
            self._show_summary()
        except Exception as e:
            self.logln(f"[error] {e}")
        finally:
            self._done_scan()

    def _match_names(self, tracks, link):
        items = [t for t in tracks if t.norm]
        try:
            from rapidfuzz import fuzz, process
            names = [t.norm for t in items]
            thr = self.name_thr.get()
            seen = set()
            for i, t in enumerate(items):
                if self._cancel:
                    return
                matches = process.extract(
                    names[i], names, scorer=fuzz.token_sort_ratio,
                    score_cutoff=thr, limit=20)
                for _m, score, j in matches:
                    if j == i:
                        continue
                    key = (min(i, j), max(i, j))
                    if key in seen:
                        continue
                    seen.add(key)
                    link(t, items[j], f"name {int(score)}%")
        except ImportError:
            # fallback: exact normalized-name match only
            by_norm = {}
            for t in items:
                by_norm.setdefault(t.norm, []).append(t)
            for grp in by_norm.values():
                for t in grp[1:]:
                    link(grp[0], t, "same name")

    def _match_audio(self, tracks, link):
        thr = self.audio_thr.get() / 100.0
        done = 0
        for t in tracks:
            if self._cancel:
                return
            t.dur, t.fp = run_fpcalc(self.fpcalc, t.path)
            done += 1
            if done % 50 == 0:
                self.logln(f"  fingerprinted {done}/{len(tracks)}")
        # bucket by integer duration, compare within +/- 2s
        buckets = {}
        for t in tracks:
            if t.fp:
                buckets.setdefault(int(round(t.dur)), []).append(t)
        keys = sorted(buckets)
        for k in keys:
            if self._cancel:
                return
            cand = []
            for kk in (k - 2, k - 1, k, k + 1, k + 2):
                cand += buckets.get(kk, [])
            for i in range(len(cand)):
                for j in range(i + 1, len(cand)):
                    a, b = cand[i], cand[j]
                    if a.path == b.path:
                        continue
                    if fp_similarity(a.fp, b.fp) >= thr:
                        link(a, b, "audio match")

    # --- output ------------------------------------------------------------
    def _report(self, folder, tracks):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        csv_path = os.path.join(folder, f"dedupe_report_{stamp}.csv")
        txt_path = os.path.join(folder, f"dedupe_report_{stamp}.txt")
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["group", "role", "reason", "file", "size_bytes",
                            "duration_s", "video_id"])
                for gi, gr in enumerate(self.groups, 1):
                    w.writerow([gi, "KEEP", gr["reason"], gr["keep"].name,
                                gr["keep"].size, int(gr["keep"].dur), gr["keep"].vid])
                    for d in gr["dups"]:
                        w.writerow([gi, "dup", gr["reason"], d.name,
                                    d.size, int(d.dur), d.vid])
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"De-dupe report  {stamp}\nFolder: {folder}\n")
                f.write(f"{len(tracks)} files, {len(self.groups)} duplicate groups\n\n")
                for gi, gr in enumerate(self.groups, 1):
                    f.write(f"Group {gi}  ({gr['reason']})\n")
                    f.write(f"   KEEP : {gr['keep'].name}\n")
                    for d in gr["dups"]:
                        f.write(f"   dup  : {d.name}\n")
                    f.write("\n")
            self.report_paths = (csv_path, txt_path)
            self.logln(f"\nReport written:\n  {os.path.basename(csv_path)}\n  {os.path.basename(txt_path)}")
        except Exception as e:
            self.logln(f"[!] Could not write report: {e}")

    def _show_summary(self):
        n_dups = sum(len(g["dups"]) for g in self.groups)
        self.logln(f"\n=== {len(self.groups)} duplicate groups, {n_dups} extra files ===\n")
        for gi, gr in enumerate(self.groups[:80], 1):
            self.logln(f"[{gi}] {gr['reason']}")
            self.logln(f"   KEEP  {gr['keep'].name}")
            for d in gr["dups"]:
                self.logln(f"   dup   {d.name}")
        if len(self.groups) > 80:
            self.logln(f"... and {len(self.groups) - 80} more groups (see report file).")
        self.status.set(f"Scan done — {len(self.groups)} groups, {n_dups} extras. "
                        "Pick an action and click Apply.")

    def _done_scan(self):
        self.root.after(0, lambda: self._set_busy(False))

    # --- apply -------------------------------------------------------------
    def apply(self):
        if not self.groups:
            return
        action = self.action_var.get()
        n_dups = sum(len(g["dups"]) for g in self.groups)
        if action == "report":
            messagebox.showinfo("Dedupe", "Report was already written during the scan. "
                                "Nothing else to do for 'Report only'.")
            return
        verb = "move" if action == "move" else "DELETE"
        if not messagebox.askyesno(
                "Dedupe", f"This will {verb} {n_dups} duplicate file(s), keeping "
                          f"{len(self.groups)} originals.\n\nContinue?"):
            return
        folder = self.dir_var.get().strip()
        self._set_busy(True)
        self.status.set("Applying...")
        threading.Thread(target=self._apply_worker, args=(action, folder),
                         daemon=True).start()

    def _apply_worker(self, action, folder):
        moved = deleted = failed = 0
        dup_dir = os.path.join(folder, "_duplicates")
        if action == "move":
            os.makedirs(dup_dir, exist_ok=True)
        self.logln(f"\n=== Apply ({action}) @ {datetime.now():%H:%M:%S} ===")
        for gr in self.groups:
            for d in gr["dups"]:
                try:
                    if action == "move":
                        dest = os.path.join(dup_dir, d.name)
                        base, ext = os.path.splitext(dest)
                        k = 1
                        while os.path.exists(dest):
                            dest = f"{base} ({k}){ext}"
                            k += 1
                        shutil.move(d.path, dest)
                        moved += 1
                    elif action == "delete":
                        os.remove(d.path)
                        deleted += 1
                except Exception as e:
                    failed += 1
                    self.logln(f"   [fail] {d.name}: {e}")
        if action == "move":
            self.logln(f"Moved {moved} files into _duplicates.")
        else:
            self.logln(f"Deleted {deleted} files.")
        if failed:
            self.logln(f"{failed} could not be processed (see above).")
        self.status.set("Apply done.")
        # groups are now stale
        self.groups = []
        self.root.after(0, lambda: self._set_busy(False))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
