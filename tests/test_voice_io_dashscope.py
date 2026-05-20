import base64
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock


class FakeResponse:
    def __init__(self, status_code=HTTPStatus.OK, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.content = text.encode("utf-8")

    def json(self):
        return self._payload


def load_voice_io_node():
    rospy = types.SimpleNamespace(
        get_param=lambda name, default=None: default,
        logdebug=lambda *args, **kwargs: None,
        logerr=lambda *args, **kwargs: None,
        logfatal=lambda *args, **kwargs: None,
        loginfo=lambda *args, **kwargs: None,
        logwarn_throttle=lambda *args, **kwargs: None,
        signal_shutdown=lambda *args, **kwargs: None,
        is_shutdown=lambda: True,
    )
    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")

    class String:
        def __init__(self, data=""):
            self.data = data

    std_msgs_msg.String = String
    std_msgs.msg = std_msgs_msg
    sys.modules.setdefault("rospy", rospy)
    sys.modules.setdefault("std_msgs", std_msgs)
    sys.modules.setdefault("std_msgs.msg", std_msgs_msg)

    module_path = Path(__file__).resolve().parents[1] / "scripts" / "voice_io_node.py"
    spec = importlib.util.spec_from_file_location("voice_io_node_for_tests", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DashScopeVoiceIOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = load_voice_io_node()

    def test_missing_dashscope_api_key_fails_fast(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
                self.node._dashscope_api_key()

    def test_save_dashscope_audio_from_base64(self):
        expected = b"fake-wav-bytes"
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            audio_path = Path(tmp_file.name)
        try:
            self.node._save_dashscope_audio({"data": base64.b64encode(expected).decode("ascii")}, audio_path)
            self.assertEqual(audio_path.read_bytes(), expected)
        finally:
            audio_path.unlink(missing_ok=True)

    def test_extract_transcription_text_from_fun_asr_shape(self):
        data = {
            "transcripts": [
                {
                    "sentences": [
                        {"text": "已经"},
                        {"text": "到达目的地"},
                    ]
                }
            ]
        }
        self.assertEqual(self.node._extract_dashscope_transcription_text(data), "已经 到达目的地")

    def test_dashscope_asr_submit_uses_fun_asr_payload(self):
        asr = self.node.DashScopeASR.__new__(self.node.DashScopeASR)
        asr._api_key = "sk-test"
        asr._api_base_url = "https://example.test/api/v1"
        asr._model = "fun-asr-mtl"
        asr._language_hints = ["zh"]
        asr._channel_id = [0]

        fake_response = FakeResponse(payload={"output": {"task_id": "task-123"}})
        with mock.patch("requests.post", return_value=fake_response) as post:
            task_id = asr._submit_task("oss://voice.wav")

        self.assertEqual(task_id, "task-123")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], "fun-asr-mtl")
        self.assertEqual(kwargs["json"]["input"]["file_urls"], ["oss://voice.wav"])
        self.assertEqual(kwargs["json"]["parameters"]["language_hints"], ["zh"])
        self.assertEqual(kwargs["headers"]["X-DashScope-Async"], "enable")

    def test_dashscope_tts_uses_configured_voice_without_creating(self):
        params = {
            "dashscope/tts_model": "qwen3-tts-vd-2026-01-26",
            "dashscope/tts_voice": "voice-test",
            "dashscope/tts_language_type": "",
            "dashscope/tts_format": "wav",
            "dashscope/workspace": "",
            "local/audio_player_cmd": [],
        }

        def fake_private_param(name, default=None):
            return params.get(name, default)

        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test"}, clear=True):
            with mock.patch.object(self.node, "_private_param", side_effect=fake_private_param):
                with mock.patch.object(self.node.DashScopeTTS, "_create_voice", side_effect=AssertionError):
                    tts = self.node.DashScopeTTS()

        self.assertEqual(tts._voice, "voice-test")


if __name__ == "__main__":
    unittest.main()
