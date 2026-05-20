import base64
import importlib.util
import os
import requests
import sys
import tempfile
import threading
import types
import unittest
from array import array
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
        asr._http_timeout_sec = 30.0

        fake_response = FakeResponse(payload={"output": {"task_id": "task-123"}})
        with mock.patch("requests.post", return_value=fake_response) as post:
            task_id = asr._submit_task("oss://voice.wav")

        self.assertEqual(task_id, "task-123")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], "fun-asr-mtl")
        self.assertEqual(kwargs["json"]["input"]["file_urls"], ["oss://voice.wav"])
        self.assertEqual(kwargs["json"]["parameters"]["language_hints"], ["zh"])
        self.assertEqual(kwargs["headers"]["X-DashScope-Async"], "enable")

    def test_dashscope_asr_poll_uses_post_task_api(self):
        asr = self.node.DashScopeASR.__new__(self.node.DashScopeASR)
        asr._api_key = "sk-test"
        asr._api_base_url = "https://example.test/api/v1"
        asr._timeout_sec = 1.0
        asr._poll_interval_sec = 0.0
        asr._http_timeout_sec = 30.0

        fake_response = FakeResponse(
            payload={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [{"transcription_url": "https://example.test/result.json"}],
                }
            }
        )
        with mock.patch("requests.post", return_value=fake_response) as post:
            result_url = asr._wait_for_result_url("task-123")

        self.assertEqual(result_url, "https://example.test/result.json")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://example.test/api/v1/tasks/task-123")

    def test_dashscope_asr_failure_is_sanitized(self):
        asr = self.node.DashScopeASR.__new__(self.node.DashScopeASR)
        asr._api_key = "sk-test"
        asr._api_base_url = "https://example.test/api/v1"
        asr._timeout_sec = 1.0
        asr._poll_interval_sec = 0.0
        asr._http_timeout_sec = 30.0

        fake_response = FakeResponse(
            payload={
                "output": {
                    "task_status": "FAILED",
                    "code": "SUCCESS_WITH_NO_VALID_FRAGMENT",
                    "message": "SUCCESS_WITH_NO_VALID_FRAGMENT",
                    "results": [
                        {
                            "file_url": "https://temporary.example/audio.wav?secret=1",
                            "subtask_status": "FAILED",
                            "code": "SUCCESS_WITH_NO_VALID_FRAGMENT",
                            "message": "SUCCESS_WITH_NO_VALID_FRAGMENT",
                        }
                    ],
                }
            }
        )
        with mock.patch("requests.post", return_value=fake_response):
            with self.assertRaisesRegex(RuntimeError, "SUCCESS_WITH_NO_VALID_FRAGMENT") as ctx:
                asr._wait_for_result_url("task-123")

        self.assertNotIn("temporary.example", str(ctx.exception))

    def test_remove_pcm_dc_offset(self):
        pcm = (1000).to_bytes(2, "little", signed=True) * 16
        cleaned = self.node._remove_pcm_dc_offset(pcm)
        self.assertEqual(self.node.audioop.rms(cleaned, 2), 0)

    def test_remove_pcm_dc_offset_preserves_little_endian_samples(self):
        samples = array("h", [1000, 1100, 900, 1000])
        if sys.byteorder != "little":
            samples.byteswap()
        cleaned = self.node._remove_pcm_dc_offset(samples.tobytes())
        out = array("h")
        out.frombytes(cleaned)
        if sys.byteorder != "little":
            out.byteswap()
        self.assertEqual(list(out), [0, 100, -100, 0])

    def test_dashscope_upload_timeout_fails_fast(self):
        asr = self.node.DashScopeASR.__new__(self.node.DashScopeASR)
        asr._api_key = "sk-test"
        asr._model = "fun-asr-mtl"
        asr._upload_timeout_sec = 0.1
        asr._http_timeout_sec = 1.0

        fake_upload_info = types.SimpleNamespace(
            status_code=HTTPStatus.OK,
            output={
                "oss_access_key_id": "ak",
                "signature": "sig",
                "policy": "policy",
                "upload_dir": "upload-dir",
                "x_oss_object_acl": "private",
                "x_oss_forbid_overwrite": "true",
                "upload_host": "https://upload.example",
            },
        )

        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp_file:
            Path(tmp_file.name).write_bytes(b"fake-wav")
            with mock.patch(
                "dashscope.utils.oss_utils.OssUtils.get_upload_certificate",
                return_value=fake_upload_info,
            ):
                with mock.patch("dashscope.common.utils.get_user_agent", return_value="ua"):
                    with mock.patch("requests.post", side_effect=requests.Timeout):
                        with self.assertRaisesRegex(RuntimeError, "upload timed out"):
                            asr._upload_audio(Path(tmp_file.name))

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


class LocalVoskStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = load_voice_io_node()

    def test_vosk_streaming_publishes_final_text(self):
        voice_node = self.node.VoiceIONode.__new__(self.node.VoiceIONode)
        voice_node.block_size = 4
        voice_node.pause_listening_while_speaking = False
        voice_node.publish_empty_result = False
        voice_node.tts_active = threading.Event()
        published = []
        voice_node.input_pub = types.SimpleNamespace(publish=lambda msg: published.append(msg.data))

        class FakeASR:
            def __init__(self):
                self.calls = 0

            def create_stream_recognizer(self):
                return object()

            def accept_stream_block(self, _recognizer, _pcm_bytes):
                self.calls += 1
                if self.calls == 2:
                    return "我 要 去 实验室"
                return None

        class FakeStream:
            def read(self, _block_size):
                return b"\x00" * 8, False

        voice_node.asr = FakeASR()
        with mock.patch.object(self.node.rospy, "is_shutdown", side_effect=[False, False, True]):
            voice_node._read_vosk_stream(FakeStream())

        self.assertEqual(published, ["我 要 去 实验室"])


if __name__ == "__main__":
    unittest.main()
