#!/usr/bin/env python3
"""A small macOS dictation helper for TapToText-style workflows."""

import argparse
from datetime import datetime, timezone
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid


OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_LOCAL_MODEL = "base"
DEFAULT_LANGUAGE = "en"
DEFAULT_CUSTOM_TERMS = "TapToText, Whisper, Codex, Wispr Flow"
DEFAULT_HOTKEY = "<cmd>+<shift>+space"
APP_DIR = Path.home() / ".taptotext"
HISTORY_PATH = APP_DIR / "history.jsonl"
IGNORED_FFMPEG_WARNINGS = (
    "AVCaptureDeviceTypeExternal",
    "AVCaptureDeviceTypeContinuityCamera",
    "Continuity Cameras",
    "NSCameraUseContinuityCameraDeviceType",
)


class TapToTextError(RuntimeError):
    """Raised for expected runtime failures with a concise user-facing message."""


class PasteBlockedError(TapToTextError):
    """Raised when transcription succeeded but macOS blocks automatic paste."""


def find_executable(name):
    found = shutil.which(name)
    if found:
        return found

    candidates = [
        Path(sys.executable).resolve().parent / name,
        Path(__file__).resolve().parent / ".venv" / "bin" / name,
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def arm64_python_available():
    if sys.platform != "darwin":
        return False

    arch_bin = Path("/usr/bin/arch")
    if not arch_bin.exists():
        return False

    result = subprocess.run(
        [
            str(arch_bin),
            "-arm64",
            sys.executable,
            "-c",
            "import platform; raise SystemExit(0 if platform.machine() == 'arm64' else 1)",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def whisper_python_command():
    if arm64_python_available():
        return ["/usr/bin/arch", "-arm64", sys.executable, "-m", "whisper"]
    return [sys.executable, "-m", "whisper"]


def normalize_custom_terms(custom_terms):
    if not custom_terms:
        return []
    if isinstance(custom_terms, (list, tuple)):
        raw_terms = custom_terms
    else:
        raw_terms = re.split(r"[,\n]", str(custom_terms))

    terms = []
    seen = set()
    for term in raw_terms:
        clean = str(term).strip()
        key = clean.casefold()
        if clean and key not in seen:
            terms.append(clean)
            seen.add(key)
    return terms


def build_initial_prompt(prompt=None, custom_terms=None):
    terms = normalize_custom_terms(custom_terms)
    parts = []
    if terms:
        parts.append(
            "Important custom words, names, product names, and technical terms: "
            + ", ".join(terms)
            + "."
        )
    if prompt and prompt.strip():
        parts.append(prompt.strip())
    return " ".join(parts) or None


def normalize_for_prompt_echo(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def looks_like_custom_terms_echo(text, custom_terms):
    terms = normalize_custom_terms(custom_terms)
    if not text or not terms:
        return False

    text_norm = normalize_for_prompt_echo(text)
    terms_norm = normalize_for_prompt_echo(" ".join(terms))
    return bool(text_norm and text_norm == terms_norm)


def postprocess_transcript(text, custom_terms=None):
    clean = text.strip()
    if looks_like_custom_terms_echo(clean, custom_terms):
        return ""
    return clean


def build_multipart_form(fields, file_field, file_path, boundary=None):
    """Build a multipart/form-data body using only the Python standard library."""
    boundary = boundary or "----taptotext-" + uuid.uuid4().hex
    lines = []

    for name, value in fields:
        if value is None:
            continue
        lines.extend(
            [
                f"--{boundary}".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"),
                b"",
                str(value).encode("utf-8"),
            ]
        )

    file_path = Path(file_path)
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    lines.extend(
        [
            f"--{boundary}".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"'
            ).encode("utf-8"),
            f"Content-Type: {content_type}".encode("utf-8"),
            b"",
            file_path.read_bytes(),
            f"--{boundary}--".encode("utf-8"),
            b"",
        ]
    )

    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def transcribe_with_openai(audio_path, model, language, prompt, timeout):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise TapToTextError("Set OPENAI_API_KEY before using the OpenAI backend.")

    fields = [
        ("model", model or DEFAULT_OPENAI_MODEL),
        ("response_format", "json"),
        ("language", language),
        ("prompt", prompt),
    ]
    body, content_type = build_multipart_form(fields, "file", audio_path)
    request = urllib.request.Request(
        OPENAI_TRANSCRIPTIONS_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TapToTextError(f"OpenAI transcription failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TapToTextError(f"OpenAI transcription failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TapToTextError(f"OpenAI returned a non-JSON response: {payload[:200]}") from exc

    text = data.get("text") if isinstance(data, dict) else None
    if not text:
        raise TapToTextError(f"OpenAI response did not include text: {payload[:200]}")
    return text.strip()


def transcribe_with_whisper_cli(audio_path, model, language, prompt, timeout, model_dir=None):
    if not find_executable("whisper"):
        raise TapToTextError(
            "The whisper CLI was not found. Install openai-whisper or use --backend openai."
        )

    with tempfile.TemporaryDirectory(prefix="taptotext-whisper-") as output_dir:
        if model_dir:
            Path(model_dir).expanduser().mkdir(parents=True, exist_ok=True)
        command = whisper_python_command() + [
            str(audio_path),
            "--model",
            model or "base",
            "--output_format",
            "txt",
            "--output_dir",
            output_dir,
            "--fp16",
            "False",
            "--condition_on_previous_text",
            "False",
            "--temperature",
            "0",
        ]
        if model_dir:
            command.extend(["--model_dir", str(Path(model_dir).expanduser())])
        if language:
            command.extend(["--language", language])
        if prompt:
            command.extend(["--initial_prompt", prompt])

        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise TapToTextError(result.stderr.strip() or "whisper CLI transcription failed.")

        transcript_path = Path(output_dir) / (Path(audio_path).stem + ".txt")
        if not transcript_path.exists():
            raise TapToTextError("whisper CLI finished without writing a transcript.")
        return transcript_path.read_text(encoding="utf-8").strip()


def transcribe_audio(audio_path, args):
    custom_terms = getattr(args, "custom_terms", None)
    prompt = build_initial_prompt(
        getattr(args, "prompt", None),
        custom_terms,
    )
    if args.backend == "openai":
        text = transcribe_with_openai(
            audio_path=audio_path,
            model=args.model or DEFAULT_OPENAI_MODEL,
            language=args.language,
            prompt=prompt,
            timeout=args.timeout,
        )
        return postprocess_transcript(text, custom_terms)
    if args.backend == "whisper-cli":
        text = transcribe_with_whisper_cli(
            audio_path=audio_path,
            model=args.model or DEFAULT_LOCAL_MODEL,
            language=args.language,
            prompt=prompt,
            timeout=args.timeout,
            model_dir=getattr(args, "model_dir", None),
        )
        return postprocess_transcript(text, custom_terms)
    raise TapToTextError(f"Unknown backend: {args.backend}")


def append_history(text, audio_path, args, action):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "backend": getattr(args, "backend", ""),
        "model": getattr(args, "model", "") or "",
        "language": getattr(args, "language", "") or "",
        "prompt": getattr(args, "prompt", "") or "",
        "custom_terms": getattr(args, "custom_terms", "") or "",
        "audio_path": str(audio_path) if audio_path else "",
        "model_dir": getattr(args, "model_dir", "") or "",
        "action": action,
        "pasted": bool(getattr(args, "paste", False)),
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=True) + "\n")
    return record


def load_history(limit=50):
    if not HISTORY_PATH.exists():
        return []

    records = []
    with HISTORY_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records[-limit:][::-1]


def clear_history():
    if HISTORY_PATH.exists():
        HISTORY_PATH.unlink()


def check_offline_backend():
    whisper_bin = find_executable("whisper")
    ffmpeg_bin = find_executable("ffmpeg")
    return {
        "ready": bool(whisper_bin and ffmpeg_bin),
        "whisper_bin": whisper_bin or "",
        "ffmpeg_bin": ffmpeg_bin or "",
        "missing": [
            name
            for name, value in [
                ("whisper", whisper_bin),
                ("ffmpeg", ffmpeg_bin),
            ]
            if not value
        ],
    }


def print_offline_check():
    status = check_offline_backend()
    print("TapToText offline check")
    print(f"ffmpeg:  {status['ffmpeg_bin'] or 'missing'}")
    print(f"whisper: {status['whisper_bin'] or 'missing'}")
    if status["ready"]:
        print("Offline transcription is ready.")
        return 0

    print("\nMissing tools: " + ", ".join(status["missing"]))
    print("Install local Whisper with:")
    print("  ./install_offline_whisper.sh")
    return 1


def clean_ffmpeg_stderr(stderr):
    if not stderr:
        return ""

    cleaned_lines = []
    for line in stderr.splitlines():
        if any(warning in line for warning in IGNORED_FFMPEG_WARNINGS):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def friendly_recording_error(stderr, fallback):
    detail = clean_ffmpeg_stderr(stderr)
    if not detail:
        return fallback

    normalized = detail.replace("\n", " ").strip()
    if normalized in {": Input/output error", "Input/output error"}:
        return (
            "No microphone input was available. Check TapToText Settings > Devices "
            "and macOS Microphone permission."
        )
    return detail


def user_facing_error(error, fallback="Something went wrong."):
    message = str(error)
    if any(warning in message for warning in IGNORED_FFMPEG_WARNINGS):
        return friendly_recording_error(message, fallback)
    return message or fallback


class FfmpegRecorder:
    def __init__(self, input_device, sample_rate, output_dir):
        self.input_device = input_device
        self.sample_rate = sample_rate
        self.output_dir = Path(output_dir).expanduser()
        self.process = None
        self.path = None

    def start(self):
        if self.process and self.process.poll() is None:
            raise TapToTextError("Recording is already running.")
        ffmpeg = find_executable("ffmpeg")
        if not ffmpeg:
            raise TapToTextError("ffmpeg is required and was not found on PATH.")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = self.output_dir / f"dictation-{timestamp}.wav"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-i",
            self.input_device,
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "-y",
            str(self.path),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        time.sleep(0.5)
        if self.process.poll() is not None:
            stderr = self.process.stderr.read().decode("utf-8", errors="replace")
            self.process = None
            detail = friendly_recording_error(
                stderr,
                "Could not start microphone recording. Check Microphone permission and input device.",
            )
            raise TapToTextError(detail)
        return self.path

    def stop(self):
        if not self.process:
            raise TapToTextError("Recording is not running.")

        process = self.process
        path = self.path
        try:
            stdout, stderr = process.communicate(input=b"q", timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
        finally:
            self.process = None
            self.path = None

        if not path or not path.exists() or path.stat().st_size < 1024:
            raw_detail = stderr.decode("utf-8", errors="replace") if stderr else ""
            detail = friendly_recording_error(
                raw_detail,
                "No usable audio was recorded. Speak for a moment before pressing Stop, "
                "and check Microphone permission if this keeps happening.",
            )
            raise TapToTextError(detail)
        return path


def list_avfoundation_devices():
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        raise TapToTextError("ffmpeg is required and was not found on PATH.")

    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        text=True,
        capture_output=True,
        check=False,
    )
    output = (result.stderr or result.stdout).strip()
    if output:
        print(output)

    if "AVFoundation audio devices:" not in output:
        return result.returncode

    audio_section = output.split("AVFoundation audio devices:", 1)[1]
    has_audio_device = any(re.search(r"\]\s+\[\d+\]", line) for line in audio_section.splitlines())
    if not has_audio_device:
        print(
            "No microphone devices were visible. Check macOS Microphone permission "
            "for Terminal, Python, or ffmpeg.",
            file=sys.stderr,
        )
    return 0


def copy_to_clipboard(text):
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def paste_clipboard():
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "v" using command down',
        ],
        check=True,
    )


def deliver_text(text, paste):
    copy_to_clipboard(text)
    if not paste:
        return "copied"

    try:
        paste_clipboard()
    except subprocess.CalledProcessError as exc:
        raise PasteBlockedError(
            "Transcript copied to clipboard, but macOS blocked automatic paste. "
            "Enable Accessibility permission for TapToText, or press Command+V "
            "in your editor manually."
        ) from exc
    return "pasted"


def process_recording(audio_path, args):
    print(f"Transcribing {audio_path.name} with {args.backend}...")
    text = transcribe_audio(audio_path, args)
    if not text:
        raise TapToTextError("The transcript was empty.")

    try:
        action = deliver_text(text, args.paste)
    except PasteBlockedError as exc:
        action = "copied"
        print(f"Warning: {exc}", file=sys.stderr)
    if not getattr(args, "no_history", False):
        append_history(text, audio_path, args, action)
    print(f"Transcript {action}: {text}")

    if args.delete_audio:
        try:
            audio_path.unlink()
        except OSError as exc:
            print(f"Warning: could not delete {audio_path}: {exc}", file=sys.stderr)


def run_once(args):
    recorder = FfmpegRecorder(args.input_device, args.sample_rate, args.output_dir)
    input("Press Enter to start recording.")
    audio_path = recorder.start()
    print("Recording. Press Enter to stop.")
    try:
        input()
    finally:
        audio_path = recorder.stop()
    process_recording(audio_path, args)


def run_daemon(args):
    try:
        from pynput import keyboard
    except ImportError as exc:
        raise TapToTextError(
            "Daemon mode requires pynput. Install it with: python -m pip install -r requirements.txt"
        ) from exc

    recorder = FfmpegRecorder(args.input_device, args.sample_rate, args.output_dir)
    lock = threading.Lock()
    state = {"recording": False, "busy": False}

    def finish(audio_path):
        try:
            process_recording(audio_path, args)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
        finally:
            with lock:
                state["busy"] = False

    def toggle_recording():
        with lock:
            if state["busy"]:
                print("Still transcribing the previous recording.")
                return

            if not state["recording"]:
                try:
                    audio_path = recorder.start()
                except Exception as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return
                state["recording"] = True
                print(f"Recording started: {audio_path.name}. Press {args.hotkey} again to stop.")
                return

            try:
                audio_path = recorder.stop()
            except Exception as exc:
                state["recording"] = False
                print(f"Error: {exc}", file=sys.stderr)
                return

            state["recording"] = False
            state["busy"] = True

        threading.Thread(target=finish, args=(audio_path,), daemon=True).start()

    print(f"TapToText daemon running. Toggle recording with {args.hotkey}.")
    print("Keep this terminal open. Press Ctrl-C here to quit.")
    with keyboard.GlobalHotKeys({args.hotkey: toggle_recording}) as listener:
        listener.join()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Record speech, transcribe it, and copy or paste the text on macOS."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="record once from this terminal")
    mode.add_argument("--daemon", action="store_true", help="run a global hotkey listener")
    mode.add_argument(
        "--list-devices",
        action="store_true",
        help="show ffmpeg avfoundation microphone device indexes",
    )
    mode.add_argument(
        "--check-offline",
        action="store_true",
        help="check whether local Whisper offline transcription is ready",
    )

    parser.add_argument(
        "--backend",
        choices=["openai", "whisper-cli"],
        default="whisper-cli",
        help="transcription backend",
    )
    parser.add_argument(
        "--model",
        help=(
            "backend model name. Defaults to gpt-4o-mini-transcribe for OpenAI "
            "and base for local whisper-cli"
        ),
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help="optional language hint, such as en or es",
    )
    parser.add_argument("--prompt", help="optional spelling/style hint for the transcription model")
    parser.add_argument(
        "--custom-terms",
        default=DEFAULT_CUSTOM_TERMS,
        help="comma-separated names, terms, or jargon to bias transcription",
    )
    parser.add_argument(
        "--model-dir",
        default="~/.taptotext/models",
        help="where local Whisper model files are stored",
    )
    parser.add_argument("--timeout", type=int, default=180, help="transcription timeout in seconds")
    parser.add_argument(
        "--input-device",
        default=":0",
        help='ffmpeg avfoundation input, usually ":0" for the first microphone',
    )
    parser.add_argument("--sample-rate", type=int, default=16000, help="recording sample rate")
    parser.add_argument(
        "--output-dir",
        default="~/.taptotext/recordings",
        help="where temporary recordings are written",
    )
    parser.add_argument(
        "--delete-audio",
        action="store_true",
        help="delete each WAV file after successful transcription",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="do not save the transcript to local history",
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        help="paste into the active app after copying to the clipboard",
    )
    parser.add_argument(
        "--hotkey",
        default=DEFAULT_HOTKEY,
        help="pynput hotkey for daemon mode",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.list_devices:
            return list_avfoundation_devices()
        if args.check_offline:
            return print_offline_check()
        if args.daemon:
            run_daemon(args)
            return 0
        run_once(args)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except TapToTextError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
