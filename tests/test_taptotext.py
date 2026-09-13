import json
from argparse import Namespace
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import taptotext
import taptotext_widget


class MultipartTests(unittest.TestCase):
    def test_build_multipart_form_includes_fields_and_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio = Path(tmpdir) / "sample.wav"
            audio.write_bytes(b"fake wav bytes")

            body, content_type = taptotext.build_multipart_form(
                fields=[
                    ("model", "gpt-4o-mini-transcribe"),
                    ("language", "en"),
                    ("prompt", None),
                ],
                file_field="file",
                file_path=audio,
                boundary="test-boundary",
            )

        self.assertEqual(content_type, "multipart/form-data; boundary=test-boundary")
        self.assertIn(b'name="model"', body)
        self.assertIn(b"gpt-4o-mini-transcribe", body)
        self.assertIn(b'name="language"', body)
        self.assertIn(b"fake wav bytes", body)
        self.assertNotIn(b'name="prompt"', body)
        self.assertTrue(body.endswith(b"--test-boundary--\r\n"))

    def test_openai_response_shape_assumption(self):
        payload = json.loads('{"text": "hello world"}')
        self.assertEqual(payload["text"], "hello world")


class OfflineHelperTests(unittest.TestCase):
    def test_default_local_bias_terms_include_project_name(self):
        self.assertEqual(taptotext.DEFAULT_LANGUAGE, "en")
        self.assertIn("TapToText", taptotext.DEFAULT_CUSTOM_TERMS)

    def test_clean_ffmpeg_stderr_removes_continuity_camera_warning(self):
        stderr = (
            "2026-09-05 ffmpeg WARNING: AVCaptureDeviceTypeExternal is deprecated "
            "for Continuity Cameras. Please use AVCaptureDeviceTypeContinuityCamera "
            "and add NSCameraUseContinuityCameraDeviceType to your Info.plist.\n"
            "real microphone error"
        )
        self.assertEqual(taptotext.clean_ffmpeg_stderr(stderr), "real microphone error")

    def test_friendly_recording_error_replaces_warning_only(self):
        stderr = (
            "2026-09-08 18:33:32.084 ffmpeg WARNING: AVCaptureDeviceTypeExternal "
            "is deprecated for Continuity Cameras. Please use "
            "AVCaptureDeviceTypeContinuityCamera and add "
            "NSCameraUseContinuityCameraDeviceType to your Info.plist."
        )
        self.assertEqual(taptotext.friendly_recording_error(stderr, "fallback"), "fallback")

    def test_user_facing_error_filters_continuity_camera_warning(self):
        message = taptotext.user_facing_error(
            "WARNING: AVCaptureDeviceTypeExternal is deprecated for Continuity Cameras.",
            "No usable audio was recorded.",
        )
        self.assertEqual(message, "No usable audio was recorded.")

    def test_build_initial_prompt_combines_custom_terms_and_prompt(self):
        prompt = taptotext.build_initial_prompt(
            prompt="Use concise punctuation.",
            custom_terms="TapToText, Whisper\nCodex, taptotext",
        )

        self.assertIn("TapToText", prompt)
        self.assertIn("Whisper", prompt)
        self.assertIn("Codex", prompt)
        self.assertEqual(prompt.count("TapToText"), 1)
        self.assertIn("Use concise punctuation.", prompt)

    def test_custom_terms_echo_is_removed(self):
        text = taptotext.postprocess_transcript("TapToText, Whisper.", "TapToText, Whisper")
        self.assertEqual(text, "")

    def test_custom_terms_plus_real_speech_is_kept(self):
        text = taptotext.postprocess_transcript(
            "TapToText should paste this into VS Code.",
            "TapToText, Whisper",
        )
        self.assertEqual(text, "TapToText should paste this into VS Code.")

    def test_deliver_text_reports_paste_block_after_copy(self):
        with mock.patch.object(taptotext, "copy_to_clipboard") as copy_mock:
            with mock.patch.object(
                taptotext,
                "paste_clipboard",
                side_effect=taptotext.subprocess.CalledProcessError(1, ["osascript"]),
            ):
                with self.assertRaises(taptotext.PasteBlockedError):
                    taptotext.deliver_text("hello", paste=True)
        copy_mock.assert_called_once_with("hello")

    def test_append_and_load_history(self):
        old_app_dir = taptotext.APP_DIR
        old_history_path = taptotext.HISTORY_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            taptotext.APP_DIR = Path(tmpdir)
            taptotext.HISTORY_PATH = Path(tmpdir) / "history.jsonl"
            args = Namespace(
                backend="whisper-cli",
                model="base",
                language="en",
                prompt="",
                custom_terms="TapToText",
                paste=True,
            )

            taptotext.append_history("hello local world", Path(tmpdir) / "a.wav", args, "pasted")
            records = taptotext.load_history()

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["text"], "hello local world")
            self.assertEqual(records[0]["backend"], "whisper-cli")
            self.assertEqual(records[0]["custom_terms"], "TapToText")
        taptotext.APP_DIR = old_app_dir
        taptotext.HISTORY_PATH = old_history_path


class WidgetGeometryTests(unittest.TestCase):
    def test_saved_small_widget_geometry_expands_to_fit_controls(self):
        geometry = taptotext_widget.normalize_widget_geometry("264x118+80+120")

        self.assertEqual(geometry, "480x136+80+120")

    def test_valid_larger_widget_geometry_is_preserved(self):
        geometry = taptotext_widget.normalize_widget_geometry("500x160+12+34")

        self.assertEqual(geometry, "500x160+12+34")


if __name__ == "__main__":
    unittest.main()
