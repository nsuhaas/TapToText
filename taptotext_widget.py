#!/usr/bin/env python3
"""Floating desktop widget for TapToText."""

from argparse import Namespace
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

import taptotext


APP_NAME = "TapToText"
CONFIG_DIR = Path.home() / ".taptotext"
CONFIG_PATH = CONFIG_DIR / "config.json"
KEYCHAIN_SERVICE = "com.taptotext.openai"
KEYCHAIN_ACCOUNT = os.environ.get("USER") or "default"
WIDGET_BG = "#f7f8fa"
WIDGET_BORDER = "#111827"
TEXT_DARK = "#111827"
TEXT_MUTED = "#4b5563"
BUTTON_IDLE = "#111827"
BUTTON_IDLE_ACTIVE = "#374151"
BUTTON_RECORDING = "#b91c1c"
BUTTON_RECORDING_ACTIVE = "#991b1b"

DEFAULT_CONFIG = {
    "offline_first_version": 2,
    "backend": "whisper-cli",
    "model": taptotext.DEFAULT_LOCAL_MODEL,
    "model_dir": "~/.taptotext/models",
    "language": taptotext.DEFAULT_LANGUAGE,
    "prompt": "",
    "custom_terms": taptotext.DEFAULT_CUSTOM_TERMS,
    "input_device": ":0",
    "sample_rate": 16000,
    "timeout": 180,
    "output_dir": "~/.taptotext/recordings",
    "paste": True,
    "delete_audio": False,
    "always_on_top": True,
    "widget_geometry": "264x118+80+120",
}


def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)

    config = dict(DEFAULT_CONFIG)
    if isinstance(loaded, dict):
        config.update({key: value for key, value in loaded.items() if key in config})
        if loaded.get("offline_first_version") is None and config["backend"] == "openai":
            config["backend"] = "whisper-cli"
            config["model"] = taptotext.DEFAULT_LOCAL_MODEL
        if loaded.get("offline_first_version", 0) < 2:
            if not config.get("language"):
                config["language"] = taptotext.DEFAULT_LANGUAGE
            if not config.get("custom_terms"):
                config["custom_terms"] = taptotext.DEFAULT_CUSTOM_TERMS
            config["offline_first_version"] = 2
    return config


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")


def security_bin():
    return shutil.which("security") or "/usr/bin/security"


def read_keychain_api_key():
    result = subprocess.run(
        [
            security_bin(),
            "find-generic-password",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def save_keychain_api_key(api_key):
    result = subprocess.run(
        [
            security_bin(),
            "add-generic-password",
            "-U",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
            api_key,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "could not write API key to Keychain"
        raise taptotext.TapToTextError(detail)


def delete_keychain_api_key():
    subprocess.run(
        [
            security_bin(),
            "delete-generic-password",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def build_args(config):
    if config["backend"] == "openai":
        api_key = os.environ.get("OPENAI_API_KEY") or read_keychain_api_key()
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if not os.environ.get("OPENAI_API_KEY"):
            raise taptotext.TapToTextError("Add your OpenAI API key in Settings.")

    return Namespace(
        backend=config["backend"],
        model=config["model"] or None,
        model_dir=config.get("model_dir", "~/.taptotext/models") or None,
        language=config["language"] or None,
        prompt=config["prompt"] or None,
        custom_terms=config.get("custom_terms", "") or None,
        timeout=int(config["timeout"]),
        input_device=config["input_device"],
        sample_rate=int(config["sample_rate"]),
        output_dir=config["output_dir"],
        delete_audio=bool(config["delete_audio"]),
        paste=bool(config["paste"]),
        no_history=False,
        hotkey="",
    )


class TapToTextWidget:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.recorder = None
        self.recording = False
        self.busy = False
        self.last_audio_path = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.last_delivery_text = ""
        self.last_delivery_at = 0.0

        self.status_var = tk.StringVar(value="Ready")
        self.detail_var = tk.StringVar(value="Tap to start")
        self.button_text = tk.StringVar(value="Tap")

        self.build_window()
        self.apply_window_settings()
        self.refresh_ready_status()

    def build_window(self):
        self.root.title(APP_NAME)
        self.root.geometry(self.config.get("widget_geometry", "264x118+80+120"))
        self.root.resizable(False, False)
        self.root.overrideredirect(True)
        self.root.configure(bg=WIDGET_BORDER)

        self.container = tk.Frame(self.root, bg=WIDGET_BG, padx=9, pady=7)
        self.container.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        header = tk.Frame(self.container, bg=WIDGET_BG, cursor="fleur")
        header.pack(fill=tk.X)
        header.bind("<ButtonPress-1>", self.start_drag)
        header.bind("<B1-Motion>", self.drag_widget)
        header.bind("<ButtonRelease-1>", self.save_widget_position)

        title = tk.Label(
            header,
            text=APP_NAME,
            bg=WIDGET_BG,
            fg=TEXT_DARK,
            font=("Helvetica Neue", 12, "bold"),
            cursor="fleur",
        )
        title.pack(side=tk.LEFT)
        title.bind("<ButtonPress-1>", self.start_drag)
        title.bind("<B1-Motion>", self.drag_widget)
        title.bind("<ButtonRelease-1>", self.save_widget_position)

        settings = tk.Button(
            header,
            text="S",
            command=self.open_settings,
            relief=tk.FLAT,
            bg="#e5e7eb",
            activebackground="#d1d5db",
            fg=TEXT_DARK,
            width=2,
            height=1,
            padx=0,
            pady=0,
        )
        settings.pack(side=tk.RIGHT)

        history = tk.Button(
            header,
            text="H",
            command=self.open_history,
            relief=tk.FLAT,
            bg="#e5e7eb",
            activebackground="#d1d5db",
            fg=TEXT_DARK,
            width=2,
            height=1,
            padx=0,
            pady=0,
        )
        history.pack(side=tk.RIGHT, padx=(0, 6))

        close = tk.Button(
            header,
            text="X",
            command=self.close,
            relief=tk.FLAT,
            bg="#e5e7eb",
            activebackground="#d1d5db",
            fg=TEXT_DARK,
            width=2,
            height=1,
            padx=0,
            pady=0,
        )
        close.pack(side=tk.RIGHT, padx=(0, 6))

        self.tap_button = tk.Button(
            self.container,
            textvariable=self.button_text,
            command=self.toggle_recording,
            relief=tk.FLAT,
            bg=BUTTON_IDLE,
            activebackground=BUTTON_IDLE_ACTIVE,
            fg="#ffffff",
            activeforeground="#ffffff",
            font=("Helvetica Neue", 20, "bold"),
            height=1,
            padx=12,
            pady=5,
        )
        self.tap_button.pack(fill=tk.X, pady=(7, 5))

        status = tk.Label(
            self.container,
            textvariable=self.status_var,
            bg=WIDGET_BG,
            fg=TEXT_DARK,
            font=("Helvetica Neue", 10, "bold"),
            anchor="w",
        )
        status.pack(fill=tk.X, side=tk.LEFT, expand=True)

        detail = tk.Label(
            self.container,
            textvariable=self.detail_var,
            bg=WIDGET_BG,
            fg=TEXT_MUTED,
            font=("Helvetica Neue", 10),
            anchor="e",
        )
        detail.pack(fill=tk.X, side=tk.RIGHT, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())

    def start_drag(self, event):
        self.drag_offset_x = event.x_root - self.root.winfo_x()
        self.drag_offset_y = event.y_root - self.root.winfo_y()

    def drag_widget(self, event):
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        self.root.geometry(f"+{x}+{y}")

    def save_widget_position(self, _event=None):
        geometry = self.root.geometry()
        if geometry:
            self.config["widget_geometry"] = geometry
            save_config(self.config)

    def apply_window_settings(self):
        self.root.attributes("-topmost", bool(self.config["always_on_top"]))

    def refresh_ready_status(self):
        if self.recording or self.busy:
            return
        if self.config["backend"] == "whisper-cli":
            status = taptotext.check_offline_backend()
            if status["ready"]:
                self.set_idle("Offline Whisper ready")
            else:
                missing = ", ".join(status["missing"])
                self.status_var.set("Offline setup needed")
                self.detail_var.set(f"Missing: {missing}")
            return

        if os.environ.get("OPENAI_API_KEY") or read_keychain_api_key():
            self.set_idle("OpenAI backend ready")
        else:
            self.status_var.set("API key needed")
            self.detail_var.set("Open Settings to add a key")

    def set_idle(self, detail="Tap to start"):
        self.busy = False
        self.recording = False
        self.button_text.set("Tap")
        self.tap_button.configure(bg=BUTTON_IDLE, activebackground=BUTTON_IDLE_ACTIVE, state=tk.NORMAL)
        self.status_var.set("Ready")
        self.detail_var.set(detail)

    def set_error(self, error):
        message = taptotext.user_facing_error(error)
        self.busy = False
        self.recording = False
        self.button_text.set("Tap")
        self.tap_button.configure(bg=BUTTON_IDLE, activebackground=BUTTON_IDLE_ACTIVE, state=tk.NORMAL)
        self.status_var.set("Needs attention")
        self.detail_var.set(message)

    def show_error(self, error, fallback="Something went wrong."):
        message = taptotext.user_facing_error(error, fallback)
        self.set_error(message)
        messagebox.showerror(APP_NAME, message)

    def toggle_recording(self):
        if self.busy:
            return

        if not self.recording:
            self.start_recording()
            return

        self.stop_recording()

    def start_recording(self):
        try:
            args = build_args(self.config)
            self.recorder = taptotext.FfmpegRecorder(
                args.input_device,
                args.sample_rate,
                args.output_dir,
            )
            audio_path = self.recorder.start()
        except Exception as exc:
            self.show_error(
                exc,
                "Could not start microphone recording. Check Microphone permission and input device.",
            )
            return

        self.recording = True
        self.last_audio_path = audio_path
        self.button_text.set("Stop")
        self.tap_button.configure(bg=BUTTON_RECORDING, activebackground=BUTTON_RECORDING_ACTIVE)
        self.status_var.set("Recording")
        self.detail_var.set(audio_path.name)

    def stop_recording(self):
        try:
            audio_path = self.recorder.stop()
        except Exception as exc:
            self.show_error(
                exc,
                "No usable audio was recorded. Speak for a moment before pressing Stop, "
                "and check Microphone permission if this keeps happening.",
            )
            return

        self.recording = False
        self.busy = True
        self.button_text.set("Wait")
        self.tap_button.configure(state=tk.DISABLED, bg="#6b7280", activebackground="#6b7280")
        self.status_var.set("Transcribing")
        self.detail_var.set("Working...")

        thread = threading.Thread(target=self.transcribe_worker, args=(audio_path,), daemon=True)
        thread.start()

    def transcribe_worker(self, audio_path):
        try:
            args = build_args(self.config)
            text = taptotext.transcribe_audio(audio_path, args)
            if not text:
                raise taptotext.TapToTextError("The transcript was empty.")
        except Exception as exc:
            self.root.after(0, lambda error=exc: self.finish_with_error(error))
            return

        self.root.after(0, lambda value=text, path=audio_path, options=args: self.deliver_result(value, path, options))

    def deliver_result(self, text, audio_path, args):
        if self.is_duplicate_delivery(text):
            self.set_idle("Duplicate skipped")
            return

        try:
            taptotext.copy_to_clipboard(text)
        except Exception as exc:
            self.finish_with_error(exc)
            return

        if not args.paste:
            self.finish_delivery(text, audio_path, args, "copied")
            return

        self.status_var.set("Pasting")
        self.detail_var.set("Returning focus to your editor...")
        self.root.withdraw()
        self.root.after(650, lambda: self.complete_auto_paste(text, audio_path, args))

    def complete_auto_paste(self, text, audio_path, args):
        try:
            taptotext.paste_clipboard()
        except subprocess.CalledProcessError as exc:
            error = taptotext.PasteBlockedError(
                "Transcript copied to clipboard, but macOS blocked automatic paste. "
                "Enable Accessibility permission for TapToText, or press Command+V "
                "in your editor manually."
            )
            self.restore_widget()
            self.finish_delivery(text, audio_path, args, "copied", error=error)
            return
        except Exception as exc:
            self.restore_widget()
            self.finish_delivery(text, audio_path, args, "copied", error=exc)
            return

        self.restore_widget()
        self.finish_delivery(text, audio_path, args, "pasted")

    def restore_widget(self):
        self.root.deiconify()
        self.apply_window_settings()
        self.root.lift()

    def is_duplicate_delivery(self, text):
        now = datetime.now().timestamp()
        normalized = " ".join(text.casefold().split())
        if normalized and normalized == self.last_delivery_text and now - self.last_delivery_at < 12:
            return True
        return False

    def mark_delivery(self, text):
        self.last_delivery_text = " ".join(text.casefold().split())
        self.last_delivery_at = datetime.now().timestamp()

    def finish_delivery(self, text, audio_path, args, action, error=None):
        try:
            taptotext.append_history(text, audio_path, args, action)
            if args.delete_audio:
                audio_path.unlink(missing_ok=True)
        except Exception as exc:
            self.finish_with_error(exc)
            return

        preview = text[:86] + ("..." if len(text) > 86 else "")
        self.mark_delivery(text)
        if error:
            self.finish_paste_blocked(error, text)
            return

        self.finish_success(action.capitalize(), preview)

    def finish_success(self, action, preview):
        self.set_idle(preview or "Tap to start")
        self.status_var.set(action)

    def finish_paste_blocked(self, error, text):
        preview = text[:86] + ("..." if len(text) > 86 else "")
        self.set_idle(preview or "Tap to start")
        self.status_var.set("Copied")
        self.detail_var.set("Auto-paste blocked; press Command+V")
        messagebox.showwarning(APP_NAME, str(error))

    def finish_with_error(self, error):
        self.show_error(error)

    def open_settings(self):
        SettingsWindow(self)

    def open_history(self):
        HistoryWindow(self)

    def close(self):
        if self.recording and self.recorder:
            try:
                self.recorder.stop()
            except Exception:
                pass
        self.root.destroy()


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title(f"{APP_NAME} Settings")
        self.window.geometry("470x545+120+160")
        self.window.configure(bg=WIDGET_BG)
        self.window.transient(app.root)

        self.vars = {
            "backend": tk.StringVar(value=app.config["backend"]),
            "model": tk.StringVar(value=app.config["model"]),
            "model_dir": tk.StringVar(value=app.config.get("model_dir", "~/.taptotext/models")),
            "language": tk.StringVar(value=app.config["language"]),
            "prompt": tk.StringVar(value=app.config["prompt"]),
            "custom_terms": tk.StringVar(value=app.config.get("custom_terms", "")),
            "input_device": tk.StringVar(value=app.config["input_device"]),
            "sample_rate": tk.StringVar(value=str(app.config["sample_rate"])),
            "timeout": tk.StringVar(value=str(app.config["timeout"])),
            "output_dir": tk.StringVar(value=app.config["output_dir"]),
            "api_key": tk.StringVar(value=""),
            "paste": tk.BooleanVar(value=bool(app.config["paste"])),
            "delete_audio": tk.BooleanVar(value=bool(app.config["delete_audio"])),
            "always_on_top": tk.BooleanVar(value=bool(app.config["always_on_top"])),
        }

        self.build()

    def build(self):
        frame = tk.Frame(self.window, bg="#f7f8fa", padx=16, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)

        self.add_entry(frame, "OpenAI API key", "api_key", show="*")
        self.add_entry(frame, "Microphone input", "input_device")
        self.add_select(frame, "Backend", "backend", ["openai", "whisper-cli"])
        self.add_entry(frame, "Model", "model")
        self.add_entry(frame, "Model folder", "model_dir")
        self.add_entry(frame, "Language", "language")
        self.add_entry(frame, "Custom terms", "custom_terms")
        self.add_entry(frame, "Style prompt", "prompt")
        self.add_entry(frame, "Sample rate", "sample_rate")
        self.add_entry(frame, "Timeout seconds", "timeout")
        self.add_entry(frame, "Recordings folder", "output_dir")

        checks = tk.Frame(frame, bg="#f7f8fa")
        checks.pack(fill=tk.X, pady=(8, 0))
        tk.Checkbutton(
            checks,
            text="Paste after transcription",
            variable=self.vars["paste"],
            bg="#f7f8fa",
            anchor="w",
        ).pack(fill=tk.X)
        tk.Checkbutton(
            checks,
            text="Delete audio after success",
            variable=self.vars["delete_audio"],
            bg="#f7f8fa",
            anchor="w",
        ).pack(fill=tk.X)
        tk.Checkbutton(
            checks,
            text="Keep widget on top",
            variable=self.vars["always_on_top"],
            bg="#f7f8fa",
            anchor="w",
        ).pack(fill=tk.X)

        buttons = tk.Frame(frame, bg="#f7f8fa")
        buttons.pack(fill=tk.X, pady=(14, 0))

        tk.Button(buttons, text="Devices", command=self.show_devices).pack(side=tk.LEFT)
        tk.Button(buttons, text="Offline Check", command=self.show_offline_status).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        tk.Button(buttons, text="Clear Key", command=self.clear_key).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side=tk.RIGHT)
        tk.Button(buttons, text="Save", command=self.save).pack(side=tk.RIGHT, padx=(0, 8))

        has_key = bool(os.environ.get("OPENAI_API_KEY") or read_keychain_api_key())
        key_status = "API key is available" if has_key else "API key is not set"
        tk.Label(frame, text=key_status, bg="#f7f8fa", fg="#4b5563", anchor="w").pack(
            fill=tk.X, pady=(10, 0)
        )

    def add_entry(self, frame, label, key, show=None):
        row = tk.Frame(frame, bg="#f7f8fa")
        row.pack(fill=tk.X, pady=3)
        tk.Label(row, text=label, bg="#f7f8fa", fg="#111827", width=17, anchor="w").pack(
            side=tk.LEFT
        )
        entry = tk.Entry(row, textvariable=self.vars[key], show=show)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return entry

    def add_select(self, frame, label, key, values):
        row = tk.Frame(frame, bg="#f7f8fa")
        row.pack(fill=tk.X, pady=3)
        tk.Label(row, text=label, bg="#f7f8fa", fg="#111827", width=17, anchor="w").pack(
            side=tk.LEFT
        )
        select = ttk.Combobox(row, textvariable=self.vars[key], values=values, state="readonly")
        select.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return select

    def save(self):
        try:
            config = {
                "backend": self.vars["backend"].get(),
                "offline_first_version": 2,
                "model": self.vars["model"].get().strip(),
                "model_dir": self.vars["model_dir"].get().strip() or "~/.taptotext/models",
                "language": self.vars["language"].get().strip(),
                "prompt": self.vars["prompt"].get().strip(),
                "custom_terms": self.vars["custom_terms"].get().strip(),
                "input_device": self.vars["input_device"].get().strip() or ":0",
                "sample_rate": int(self.vars["sample_rate"].get()),
                "timeout": int(self.vars["timeout"].get()),
                "output_dir": self.vars["output_dir"].get().strip() or "~/.taptotext/recordings",
                "paste": bool(self.vars["paste"].get()),
                "delete_audio": bool(self.vars["delete_audio"].get()),
                "always_on_top": bool(self.vars["always_on_top"].get()),
                "widget_geometry": self.app.root.geometry(),
            }
        except ValueError:
            messagebox.showerror(APP_NAME, "Sample rate and timeout must be numbers.")
            return

        api_key = self.vars["api_key"].get().strip()
        if api_key:
            try:
                save_keychain_api_key(api_key)
                os.environ["OPENAI_API_KEY"] = api_key
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return

        save_config(config)
        self.app.config = config
        self.app.apply_window_settings()
        self.app.set_idle("Settings saved")
        self.app.refresh_ready_status()
        self.window.destroy()

    def clear_key(self):
        delete_keychain_api_key()
        os.environ.pop("OPENAI_API_KEY", None)
        self.vars["api_key"].set("")
        messagebox.showinfo(APP_NAME, "Saved API key cleared.")

    def show_devices(self):
        ffmpeg = taptotext.find_executable("ffmpeg")
        if not ffmpeg:
            messagebox.showerror(APP_NAME, "ffmpeg was not found on PATH.")
            return

        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            text=True,
            capture_output=True,
            check=False,
        )
        output = taptotext.clean_ffmpeg_stderr(result.stderr or result.stdout).strip()
        if not output:
            output = "No microphone device output. Check macOS Microphone permission."

        devices = tk.Toplevel(self.window)
        devices.title("Microphone Devices")
        devices.geometry("680x380+160+200")
        text = tk.Text(devices, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True)
        text.insert("1.0", output)
        text.configure(state=tk.DISABLED)

    def show_offline_status(self):
        status = taptotext.check_offline_backend()
        if status["ready"]:
            message = (
                "Offline transcription is ready.\n\n"
                f"ffmpeg: {status['ffmpeg_bin']}\n"
                f"whisper: {status['whisper_bin']}"
            )
            messagebox.showinfo(APP_NAME, message)
            return

        message = (
            "Offline transcription is not ready.\n\n"
            f"ffmpeg: {status['ffmpeg_bin'] or 'missing'}\n"
            f"whisper: {status['whisper_bin'] or 'missing'}\n\n"
            "Install local Whisper from Terminal:\n"
            "cd /Users/suhaasn/ST/TapToText\n"
            ". .venv/bin/activate\n"
            "./install_offline_whisper.sh"
        )
        messagebox.showerror(APP_NAME, message)


class HistoryWindow:
    def __init__(self, app):
        self.app = app
        self.records = taptotext.load_history(limit=100)
        self.window = tk.Toplevel(app.root)
        self.window.title(f"{APP_NAME} History")
        self.window.geometry("760x460+140+180")
        self.window.configure(bg="#f7f8fa")
        self.window.transient(app.root)
        self.build()

    def build(self):
        frame = tk.Frame(self.window, bg="#f7f8fa", padx=14, pady=12)
        frame.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(frame, bg="#f7f8fa")
        left.pack(side=tk.LEFT, fill=tk.Y)

        self.listbox = tk.Listbox(left, width=36, height=20, exportselection=False)
        self.listbox.pack(fill=tk.Y, expand=False)
        self.listbox.bind("<<ListboxSelect>>", self.show_selected)

        right = tk.Frame(frame, bg="#f7f8fa")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        self.text = tk.Text(right, wrap=tk.WORD, height=16)
        self.text.pack(fill=tk.BOTH, expand=True)

        buttons = tk.Frame(right, bg="#f7f8fa")
        buttons.pack(fill=tk.X, pady=(10, 0))

        tk.Button(buttons, text="Copy", command=self.copy_selected).pack(side=tk.LEFT)
        tk.Button(buttons, text="Paste", command=self.paste_selected).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(buttons, text="Clear History", command=self.clear_all).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        tk.Button(buttons, text="Close", command=self.window.destroy).pack(side=tk.RIGHT)

        self.populate()

    def populate(self):
        self.listbox.delete(0, tk.END)
        self.text.delete("1.0", tk.END)
        if not self.records:
            self.listbox.insert(tk.END, "No transcripts yet")
            self.text.insert("1.0", "Transcripts will appear here after you use TapToText.")
            return

        for record in self.records:
            self.listbox.insert(tk.END, self.label_for(record))
        self.listbox.selection_set(0)
        self.show_record(self.records[0])

    def label_for(self, record):
        timestamp = record.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
            stamp = dt.strftime("%b %d %I:%M %p")
        except ValueError:
            stamp = "Unknown time"
        text = " ".join(str(record.get("text", "")).split())
        return f"{stamp} - {text[:28]}"

    def selected_record(self):
        selection = self.listbox.curselection()
        if not selection or not self.records:
            return None
        index = selection[0]
        if index >= len(self.records):
            return None
        return self.records[index]

    def show_selected(self, _event=None):
        record = self.selected_record()
        if record:
            self.show_record(record)

    def show_record(self, record):
        details = [
            f"Time: {record.get('timestamp', '')}",
            f"Backend: {record.get('backend', '')}",
            f"Model: {record.get('model', '')}",
            f"Audio: {record.get('audio_path', '')}",
            "",
            record.get("text", ""),
        ]
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", "\n".join(details))

    def copy_selected(self):
        record = self.selected_record()
        if not record:
            return
        taptotext.copy_to_clipboard(record.get("text", ""))
        self.app.set_idle("History copied")

    def paste_selected(self):
        record = self.selected_record()
        if not record:
            return
        taptotext.deliver_text(record.get("text", ""), paste=True)
        self.app.set_idle("History pasted")

    def clear_all(self):
        if not messagebox.askyesno(APP_NAME, "Clear all local transcript history?"):
            return
        taptotext.clear_history()
        self.records = []
        self.populate()
        self.app.set_idle("History cleared")


def main():
    root = tk.Tk()
    TapToTextWidget(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
