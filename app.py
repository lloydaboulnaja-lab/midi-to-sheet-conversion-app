"""
Muscribe — MIDI to Sheet Music Converter
Dependencies:
    pip install customtkinter pillow mido music21 pygame

Install LilyPond separately:
    https://lilypond.org/

Notes:
- Designed primarily for downloaded solo-piano MIDI files.
- Quantisation is configurable; inspect the result for expressive/swing music.
- LilyPond is required for high-quality PDF and PNG engraving.
"""

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageOps
import mido
import music21

try:
    import pygame
except ImportError:
    pygame = None


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = APP_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

COLOR_BG = "#090D14"
COLOR_PANEL = "#151D2B"
COLOR_PANEL_ALT = "#0F1724"
COLOR_ACCENT = "#4BC6B9"
COLOR_ACCENT_HOVER = "#36A99E"
COLOR_TEXT = "#F8FAFC"
COLOR_MUTED = "#9CA3AF"
COLOR_BORDER = "#293548"
COLOR_ERROR = "#F87171"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def locate_lilypond():
    """Find LilyPond in the project, PATH, or common Windows locations."""
    candidates = [
        APP_DIR / "lilypond" / "bin" / "lilypond.exe",
        Path(r"C:\Program Files\LilyPond\bin\lilypond.exe"),
        Path(r"C:\Program Files (x86)\LilyPond\bin\lilypond.exe"),
        Path(r"C:\lilypond\bin\lilypond.exe"),
        Path.home() / "lilypond" / "bin" / "lilypond.exe",
        Path.home() / "Downloads" / "lilypond" / "bin" / "lilypond.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("lilypond") or shutil.which("lilypond.exe")
    return str(Path(found).resolve()) if found else None


DETECTED_LILYPOND = locate_lilypond()


def inspect_midi(path):
    """Return basic MIDI statistics, counting note-on events with velocity > 0."""
    midi = mido.MidiFile(str(path))
    note_count = sum(
        1 for track in midi.tracks
        for msg in track
        if msg.type == "note_on" and msg.velocity > 0
    )
    return {
        "duration": midi.length,
        "tracks": len(midi.tracks),
        "notes": note_count,
        "ticks_per_beat": midi.ticks_per_beat,
    }


def quantise_score(score, mode="Balanced"):
    """
    Quantise a score using music21's supported API.

    quarterLengthDivisors describes subdivisions per quarter note:
      2 = eighth-note grid
      3 = triplet grid
      4 = sixteenth-note grid
      6 = finer compound subdivisions
      8 = thirty-second-note grid

    Quantise the complete score consistently so parts remain aligned.
    """
    grids = {
        "Gentle": (2,),
        "Balanced": (2, 3, 4),
        "Detailed": (2, 3, 4, 6, 8),
        "Off": None,
    }
    divisors = grids.get(mode, grids["Balanced"])
    if divisors is None:
        return score

    # Stream.quantize supports processOffsets, processDurations,
    # inPlace and recurse in current music21 releases.
    return score.quantize(
        quarterLengthDivisors=divisors,
        processOffsets=True,
        processDurations=True,
        inPlace=False,
        recurse=True,
    )


def ensure_time_signature(score, fallback="4/4"):
    """Add a fallback time signature only when none exists in the score."""
    existing = list(score.recurse().getElementsByClass(music21.meter.TimeSignature))
    if not existing:
        score.insert(0, music21.meter.TimeSignature(fallback))
    return score


def lilypond_quote(value):
    """Escape a value for use inside a LilyPond quoted string."""
    value = str(value or "").replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("\r", " ").replace("\n", " ")
    return value


def prepare_lilypond_source(ly_path, title, composer):
    """Insert metadata and paper settings into music21-generated LilyPond."""
    source = ly_path.read_text(encoding="utf-8")

    # Remove existing generated header/paper blocks to avoid conflicting settings.
    source = re.sub(r"\\header\s*\{.*?\}", "", source, count=1, flags=re.DOTALL)
    source = re.sub(r"\\paper\s*\{.*?\}", "", source, count=1, flags=re.DOTALL)

    header = (
        '\\header {\n'
        f'  title = "{lilypond_quote(title)}"\n'
        f'  composer = "{lilypond_quote(composer)}"\n'
        '  tagline = "Engraved with Muscribe"\n'
        '}\n\n'
    )
    paper = (
        '\\paper {\n'
        '  top-margin = 12\\mm\n'
        '  bottom-margin = 12\\mm\n'
        '  left-margin = 14\\mm\n'
        '  right-margin = 14\\mm\n'
        '  indent = 0\\mm\n'
        '  ragged-last = ##f\n'
        '}\n\n'
    )
    ly_path.write_text(header + paper + source, encoding="utf-8")


def clean_old_outputs(prefix):
    """Remove only this application's previous render artifacts."""
    for pattern in (
        f"{prefix.name}*.png",
        f"{prefix.name}*.pdf",
        f"{prefix.name}*.ps",
    ):
        for old in prefix.parent.glob(pattern):
            try:
                old.unlink()
            except OSError:
                pass


class MetadataModal(ctk.CTkToplevel):
    def __init__(self, parent, metadata, callback):
        super().__init__(parent)
        self.title("Edit Score Metadata")
        self.geometry("440x300")
        self.resizable(False, False)
        self.configure(fg_color=COLOR_PANEL)
        self.callback = callback
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(
            self, text="Score metadata",
            font=ctk.CTkFont(size=19, weight="bold"),
            text_color=COLOR_TEXT
        ).pack(anchor="w", padx=22, pady=(22, 18))

        ctk.CTkLabel(self, text="Title", text_color=COLOR_MUTED).pack(
            anchor="w", padx=22
        )
        self.title_entry = ctk.CTkEntry(
            self, width=394, fg_color=COLOR_PANEL_ALT,
            border_color=COLOR_ACCENT
        )
        self.title_entry.pack(padx=22, pady=(4, 12))
        self.title_entry.insert(0, metadata.get("title", "Untitled Score"))

        ctk.CTkLabel(self, text="Composer", text_color=COLOR_MUTED).pack(
            anchor="w", padx=22
        )
        self.composer_entry = ctk.CTkEntry(
            self, width=394, fg_color=COLOR_PANEL_ALT,
            border_color=COLOR_ACCENT
        )
        self.composer_entry.pack(padx=22, pady=(4, 18))
        self.composer_entry.insert(0, metadata.get("composer", "Unknown Composer"))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=22, pady=(0, 18))

        ctk.CTkButton(
            buttons, text="Cancel", fg_color="#354154",
            command=self.destroy
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            buttons, text="Save", fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER, text_color="#071014",
            command=self.save
        ).pack(side="right")

    def save(self):
        self.callback({
            "title": self.title_entry.get().strip() or "Untitled Score",
            "composer": self.composer_entry.get().strip() or "Unknown Composer",
        })
        self.destroy()


class MuscribeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Muscribe — MIDI to Sheet Music")
        self.geometry("1180x800")
        self.minsize(900, 620)
        self.configure(fg_color=COLOR_BG)

        self.midi_path = None
        self.rendered_sheet_paths = []
        self.rendered_pdf_path = None
        self.score_metadata = {"title": "Untitled Score", "composer": "Unknown Composer"}
        self.is_playing = False
        self.audio_ready = False
        self.render_running = False
        self.preview_images = []  # Keep CTkImage references alive.
        self.preview_width = 820

        self._init_audio()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _init_audio(self):
        if pygame is None:
            return
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
            self.audio_ready = True
        except Exception as exc:
            print(f"Audio unavailable: {exc}")
            self.audio_ready = False

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(
            self, width=285, fg_color=COLOR_PANEL, corner_radius=0
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=22, pady=(24, 24))
        ctk.CTkLabel(
            brand, text="Mus", font=ctk.CTkFont(size=26, weight="bold"),
            text_color=COLOR_TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            brand, text="cribe", font=ctk.CTkFont(size=26, weight="bold"),
            text_color=COLOR_ACCENT
        ).pack(side="left")
        ctk.CTkLabel(
            brand, text="MIDI → SCORE", font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLOR_MUTED
        ).pack(side="left", padx=(10, 0), pady=(10, 0))

        self.select_button = ctk.CTkButton(
            self.sidebar, text="📁  Select MIDI File",
            height=42, fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            text_color="#061014", font=ctk.CTkFont(size=13, weight="bold"),
            command=self.open_midi_file
        )
        self.select_button.pack(fill="x", padx=20, pady=(0, 12))

        self.info_box = ctk.CTkTextbox(
            self.sidebar, height=135, fg_color=COLOR_PANEL_ALT,
            text_color=COLOR_TEXT, border_color=COLOR_BORDER,
            font=ctk.CTkFont(size=11)
        )
        self.info_box.pack(fill="x", padx=20, pady=(0, 12))
        self._set_info("Select a MIDI file to inspect its duration, tracks and notes.")

        ctk.CTkButton(
            self.sidebar, text="✏️  Edit Score Metadata",
            fg_color="#354154", hover_color="#43536A",
            command=self.open_metadata_modal
        ).pack(fill="x", padx=20, pady=5)

        ctk.CTkLabel(
            self.sidebar, text="RHYTHM CLEANUP",
            text_color=COLOR_MUTED, font=ctk.CTkFont(size=10, weight="bold")
        ).pack(anchor="w", padx=22, pady=(18, 5))

        self.rhythm_grid = ctk.StringVar(value="Balanced")
        self.grid_menu = ctk.CTkOptionMenu(
            self.sidebar,
            values=["Off", "Gentle", "Balanced", "Detailed"],
            variable=self.rhythm_grid,
            fg_color="#354154", button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER,
            text_color=COLOR_TEXT
        )
        self.grid_menu.pack(fill="x", padx=20, pady=(0, 4))

        ctk.CTkLabel(
            self.sidebar,
            text="Balanced is a good starting point for downloaded piano MIDI.",
            text_color=COLOR_MUTED, wraplength=235, justify="left",
            font=ctk.CTkFont(size=10)
        ).pack(anchor="w", padx=22, pady=(0, 10))

        self.convert_button = ctk.CTkButton(
            self.sidebar, text="⚙️  Convert to Sheet Music",
            height=42, fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            text_color="#061014", font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_transcription, state="disabled"
        )
        self.convert_button.pack(fill="x", padx=20, pady=5)

        self.export_button = ctk.CTkButton(
            self.sidebar, text="📥  Export PDF",
            height=40, fg_color="#354154", hover_color="#43536A",
            command=self.export_pdf, state="disabled"
        )
        self.export_button.pack(fill="x", padx=20, pady=5)

        audio_frame = ctk.CTkFrame(self.sidebar, fg_color=COLOR_PANEL_ALT, corner_radius=10)
        audio_frame.pack(fill="x", padx=20, pady=(18, 8))
        ctk.CTkLabel(
            audio_frame, text="MIDI Playback",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_TEXT
        ).pack(pady=(12, 8))

        audio_buttons = ctk.CTkFrame(audio_frame, fg_color="transparent")
        audio_buttons.pack(pady=(0, 12))
        self.play_button = ctk.CTkButton(
            audio_buttons, text="▶ Play", width=95,
            fg_color="#354154", command=self.toggle_play, state="disabled"
        )
        self.play_button.pack(side="left", padx=4)
        self.stop_button = ctk.CTkButton(
            audio_buttons, text="■ Stop", width=95,
            fg_color="#354154", command=self.stop_audio, state="disabled"
        )
        self.stop_button.pack(side="left", padx=4)

        if not self.audio_ready:
            self.play_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")

        self.status_label = ctk.CTkLabel(
            self.sidebar,
            text=self._initial_status(),
            text_color=COLOR_ACCENT if DETECTED_LILYPOND else COLOR_ERROR,
            wraplength=240, justify="left",
            font=ctk.CTkFont(size=10)
        )
        self.status_label.pack(side="bottom", fill="x", padx=20, pady=18)

        self.main_view = ctk.CTkFrame(self, fg_color=COLOR_BG, corner_radius=0)
        self.main_view.grid(row=0, column=1, sticky="nsew", padx=(12, 12), pady=12)
        self.main_view.grid_columnconfigure(0, weight=1)
        self.main_view.grid_rowconfigure(1, weight=1)

        topbar = ctk.CTkFrame(self.main_view, fg_color=COLOR_PANEL, corner_radius=12)
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        topbar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            topbar, text="Sheet Music Preview",
            font=ctk.CTkFont(size=17, weight="bold"), text_color=COLOR_TEXT
        ).grid(row=0, column=0, sticky="w", padx=18, pady=14)

        ctk.CTkButton(
            topbar, text="−", width=34, fg_color="#354154",
            command=lambda: self.change_preview_width(-100)
        ).grid(row=0, column=1, padx=(0, 5), pady=8)
        ctk.CTkButton(
            topbar, text="Zoom", width=55, fg_color="#354154",
            command=lambda: self.change_preview_width(0, reset=True)
        ).grid(row=0, column=2, padx=4, pady=8)
        ctk.CTkButton(
            topbar, text="+", width=34, fg_color="#354154",
            command=lambda: self.change_preview_width(100)
        ).grid(row=0, column=3, padx=(5, 12), pady=8)

        self.preview = ctk.CTkScrollableFrame(
            self.main_view, fg_color=COLOR_PANEL, corner_radius=12
        )
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.placeholder = ctk.CTkLabel(
            self.preview,
            text="No sheet music yet\n\nSelect a MIDI file, choose a rhythm grid,\nand click Convert to Sheet Music.",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=14), justify="center"
        )
        self.placeholder.pack(expand=True, fill="both", padx=30, pady=120)

    def _initial_status(self):
        if DETECTED_LILYPOND:
            return "Ready • LilyPond detected"
        return "LilyPond not found • Install it to render scores"

    def update_status(self, message, error=False):
        self.status_label.configure(
            text=message,
            text_color=COLOR_ERROR if error else COLOR_ACCENT
        )

    def _set_info(self, text):
        self.info_box.configure(state="normal")
        self.info_box.delete("1.0", "end")
        self.info_box.insert("1.0", text)
        self.info_box.configure(state="disabled")

    def open_midi_file(self):
        path = filedialog.askopenfilename(
            title="Choose a MIDI file",
            filetypes=[("MIDI files", "*.mid *.midi")]
        )
        if not path:
            return

        try:
            stats = inspect_midi(path)
        except Exception as exc:
            messagebox.showerror("Invalid MIDI", f"Could not read this MIDI file:\n\n{exc}")
            return

        self.stop_audio()
        self.midi_path = str(Path(path).resolve())
        self.score_metadata = {
            "title": Path(path).stem.replace("_", " ").replace("-", " "),
            "composer": "Unknown Composer",
        }
        self.rendered_sheet_paths = []
        self.rendered_pdf_path = None
        self.export_button.configure(state="disabled")
        self.convert_button.configure(state="normal")
        self._set_info(
            f"File: {Path(path).name}\n"
            f"Duration: {stats['duration']:.1f} seconds\n"
            f"Tracks: {stats['tracks']}\n"
            f"Note-on events: {stats['notes']}\n"
            f"Ticks per beat: {stats['ticks_per_beat']}\n"
        )
        self._clear_preview("Ready to convert\n\nChoose a rhythm cleanup level and click Convert.")
        self.update_status(f"Loaded {Path(path).name}")

        if self.audio_ready:
            try:
                pygame.mixer.music.load(self.midi_path)
                self.play_button.configure(state="normal")
                self.stop_button.configure(state="normal")
            except Exception as exc:
                self.play_button.configure(state="disabled")
                self.stop_button.configure(state="disabled")
                print(f"Playback load failed: {exc}")

    def open_metadata_modal(self):
        if not self.midi_path:
            messagebox.showinfo("No MIDI selected", "Select a MIDI file first.")
            return
        MetadataModal(self, self.score_metadata, self.update_metadata)

    def update_metadata(self, metadata):
        self.score_metadata = metadata
        self.update_status("Score metadata updated")

    def start_transcription(self):
        if self.render_running:
            return
        if not self.midi_path:
            messagebox.showwarning("No MIDI selected", "Select a MIDI file first.")
            return
        if not DETECTED_LILYPOND:
            messagebox.showerror(
                "LilyPond not found",
                "Install LilyPond, add it to PATH, or place it in:\n"
                "your-project/lilypond/bin/lilypond.exe"
            )
            self.update_status("LilyPond is required to render scores", error=True)
            return

        self.render_running = True
        self.convert_button.configure(state="disabled", text="Rendering…")
        self.export_button.configure(state="disabled")
        # Snapshot GUI state before starting the worker.
        midi_path = self.midi_path
        metadata = dict(self.score_metadata)
        grid = self.rhythm_grid.get()
        threading.Thread(
            target=self._render_worker,
            args=(midi_path, metadata, grid),
            daemon=True
        ).start()

    def _render_worker(self, midi_path, metadata, grid):
        try:
            prefix = OUTPUT_DIR / "muscribe_score"
            ly_path = OUTPUT_DIR / "muscribe_score.ly"
            self.after(0, self.update_status, "Parsing MIDI…")
            clean_old_outputs(prefix)

            # Parse source. Keep its part structure rather than flattening it.
            score = music21.converter.parse(midi_path)
            if not score.recurse().notes:
                raise ValueError("This MIDI contains no pitched notes to engrave.")

            self.after(0, self.update_status, f"Cleaning rhythm ({grid.lower()})…")
            score = quantise_score(score, grid)
            score = ensure_time_signature(score)

            # Measure creation must happen after rhythm cleanup.
            score = score.makeMeasures(inPlace=False)
            score = score.makeNotation(inPlace=False)

            if score.metadata is None:
                score.metadata = music21.metadata.Metadata()
            score.metadata.title = metadata.get("title", "Untitled Score")
            score.metadata.composer = metadata.get("composer", "Unknown Composer")

            self.after(0, self.update_status, "Writing LilyPond source…")
            score.write("lilypond", fp=str(ly_path))
            if not ly_path.exists() or ly_path.stat().st_size == 0:
                raise RuntimeError("music21 did not generate a LilyPond source file.")

            prepare_lilypond_source(
                ly_path,
                metadata.get("title", "Untitled Score"),
                metadata.get("composer", "Unknown Composer")
            )

            self.after(0, self.update_status, "Engraving PDF and preview pages…")
            command = [
                DETECTED_LILYPOND,
                "--png",
                "-dno-point-and-click",
                "-o", str(prefix),
                str(ly_path),
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(OUTPUT_DIR)
            )

            pdf_path = OUTPUT_DIR / "muscribe_score.pdf"
            png_paths = sorted(OUTPUT_DIR.glob("muscribe_score*.png"))
            png_paths = [
                p for p in png_paths
                if "preview" not in p.stem.lower()
            ]

            if not pdf_path.exists() and not png_paths:
                details = (result.stderr or result.stdout or "").strip()
                if not details:
                    details = f"LilyPond exited with code {result.returncode}."
                raise RuntimeError(details[-2500:])

            self.rendered_pdf_path = str(pdf_path) if pdf_path.exists() else None
            self.rendered_sheet_paths = [str(p) for p in png_paths]

            if not png_paths:
                # PDF still exports successfully; preview requires PNG output.
                self.after(
                    0, self._clear_preview,
                    "PDF rendered successfully, but no PNG preview pages were generated."
                )
            else:
                self.after(0, self.display_pages, self.rendered_sheet_paths)

            if result.returncode != 0:
                print("LilyPond warning:", result.stderr[-2500:])

            if self.rendered_pdf_path:
                self.after(0, self.export_button.configure, {"state": "normal"})
            self.after(0, self.update_status, "Conversion complete")

        except subprocess.TimeoutExpired:
            self.after(0, self.update_status, "LilyPond timed out after 180 seconds", True)
            self.after(
                0, messagebox.showerror,
                "Rendering timed out",
                "LilyPond took longer than 180 seconds. Try a shorter MIDI file."
            )
        except Exception as exc:
            print("Muscribe render error:", repr(exc))
            self.after(0, self.update_status, f"Conversion failed: {str(exc)[:220]}", True)
            self.after(
                0, messagebox.showerror,
                "Conversion failed",
                f"{str(exc)[:2500]}"
            )
        finally:
            self.after(0, self._render_finished)

    def _render_finished(self):
        self.render_running = False
        self.convert_button.configure(
            state="normal" if self.midi_path else "disabled",
            text="⚙️  Convert to Sheet Music"
        )

    def _clear_preview(self, message):
        for widget in self.preview.winfo_children():
            widget.destroy()
        self.preview_images.clear()
        self.placeholder = ctk.CTkLabel(
            self.preview, text=message,
            text_color=COLOR_MUTED, font=ctk.CTkFont(size=14),
            justify="center"
        )
        self.placeholder.pack(expand=True, fill="both", padx=25, pady=100)

    def display_pages(self, paths):
        self._clear_preview("")
        for index, path in enumerate(paths, start=1):
            try:
                with Image.open(path) as opened:
                    image = opened.convert("RGB")
                width = min(self.preview_width, max(400, self.preview.winfo_width() - 60))
                ratio = width / image.width
                height = max(1, round(image.height * ratio))
                image = image.resize((width, height), Image.Resampling.LANCZOS)
                ctk_image = ctk.CTkImage(
                    light_image=image, dark_image=image,
                    size=(width, height)
                )
                self.preview_images.append(ctk_image)
                page = ctk.CTkFrame(self.preview, fg_color="transparent")
                page.pack(fill="x", padx=12, pady=12)
                ctk.CTkLabel(
                    page, text=f"Page {index}",
                    text_color=COLOR_MUTED, font=ctk.CTkFont(size=11)
                ).pack(anchor="w", pady=(0, 5))
                ctk.CTkLabel(page, text="", image=ctk_image).pack()
            except Exception as exc:
                print(f"Could not preview {path}: {exc}")

    def change_preview_width(self, amount, reset=False):
        self.preview_width = 820 if reset else max(500, min(1400, self.preview_width + amount))
        if self.rendered_sheet_paths:
            self.display_pages(self.rendered_sheet_paths)

    def toggle_play(self):
        if not self.audio_ready or not self.midi_path:
            return
        try:
            if self.is_playing:
                pygame.mixer.music.pause()
                self.is_playing = False
                self.play_button.configure(text="▶ Resume")
                self.update_status("Playback paused")
            else:
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.unpause()
                else:
                    pygame.mixer.music.play()
                self.is_playing = True
                self.play_button.configure(text="Ⅱ Pause")
                self.update_status("Playing MIDI")
        except Exception as exc:
            messagebox.showerror("Playback error", str(exc))

    def stop_audio(self):
        if self.audio_ready:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        self.is_playing = False
        if hasattr(self, "play_button"):
            self.play_button.configure(text="▶ Play")

    def export_pdf(self):
        if not self.rendered_pdf_path or not Path(self.rendered_pdf_path).is_file():
            messagebox.showerror("No PDF", "Convert a MIDI file successfully before exporting.")
            return

        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_",
                            self.score_metadata.get("title", "Muscribe Score")).strip(" .")
        if not safe_title:
            safe_title = "Muscribe Score"

        destination = filedialog.asksaveasfilename(
            title="Export sheet music",
            defaultextension=".pdf",
            filetypes=[("PDF document", "*.pdf")],
            initialfile=f"{safe_title}.pdf"
        )
        if not destination:
            return
        try:
            shutil.copy2(self.rendered_pdf_path, destination)
            messagebox.showinfo("Export complete", f"PDF saved to:\n{destination}")
            self.update_status("PDF exported successfully")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def on_close(self):
        self.stop_audio()
        if self.audio_ready:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = MuscribeApp()
    app.mainloop()
#for security