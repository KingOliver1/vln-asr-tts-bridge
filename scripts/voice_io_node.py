#!/usr/bin/env python3
import audioop
import json
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from pathlib import Path

import rospy
from std_msgs.msg import String


def _private_param(name, default=None):
    return rospy.get_param("~" + name, default)


def _command_param(name, default):
    value = _private_param(name, default)
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    return [str(item) for item in value]


def _expand_command(parts, **replacements):
    expanded = []
    for part in parts:
        value = part
        for key, replacement in replacements.items():
            value = value.replace("{" + key + "}", str(replacement))
        expanded.append(value)
    return expanded


def _write_wav(path, pcm_bytes, sample_rate, channels):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)


class LocalVoskASR:
    def __init__(self, sample_rate):
        try:
            import vosk
        except ImportError as exc:
            raise RuntimeError("local ASR requires the vosk Python package in the conda env") from exc

        model_path = _private_param("local/vosk_model_path", "")
        if not model_path:
            raise RuntimeError("~local/vosk_model_path is required when asr_backend=local")
        if not os.path.isdir(model_path):
            raise RuntimeError("Vosk model path does not exist: {}".format(model_path))

        self._vosk = vosk
        self._model = vosk.Model(model_path)
        self._sample_rate = sample_rate

    def transcribe(self, pcm_bytes):
        recognizer = self._vosk.KaldiRecognizer(self._model, self._sample_rate)
        recognizer.AcceptWaveform(pcm_bytes)
        result = json.loads(recognizer.FinalResult())
        return result.get("text", "").strip()


class OpenAIASR:
    def __init__(self, sample_rate, channels):
        self._sample_rate = sample_rate
        self._channels = channels
        self._client = _make_openai_client()
        self._model = _private_param("openai/asr_model", "gpt-4o-mini-transcribe")
        self._language = _private_param("language", "zh")

    def transcribe(self, pcm_bytes):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            wav_path = Path(tmp_file.name)
        try:
            _write_wav(wav_path, pcm_bytes, self._sample_rate, self._channels)
            with wav_path.open("rb") as audio_file:
                kwargs = {
                    "model": self._model,
                    "file": audio_file,
                }
                if self._language:
                    kwargs["language"] = self._language
                result = self._client.audio.transcriptions.create(**kwargs)
            return getattr(result, "text", "").strip()
        finally:
            wav_path.unlink(missing_ok=True)


class LocalTTS:
    def __init__(self):
        self._piper_executable = _private_param("local/piper_executable", "piper")
        self._piper_model_path = _private_param("local/piper_model_path", "")
        self._piper_config_path = _private_param("local/piper_config_path", "")
        self._audio_player_cmd = _command_param(
            "local/audio_player_cmd",
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "{audio_file}"],
        )
        self._fallback_tts_cmd = _command_param("local/fallback_tts_cmd", ["spd-say", "{text}"])

    def speak(self, text):
        if self._piper_model_path:
            self._speak_with_piper(text)
            return
        if self._fallback_tts_cmd:
            cmd = _expand_command(self._fallback_tts_cmd, text=text)
            _run_checked(cmd, "fallback TTS")
            return
        raise RuntimeError("local TTS needs ~local/piper_model_path or ~local/fallback_tts_cmd")

    def _speak_with_piper(self, text):
        if not os.path.isfile(self._piper_model_path):
            raise RuntimeError("Piper model path does not exist: {}".format(self._piper_model_path))

        executable = shutil.which(self._piper_executable) or self._piper_executable
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            wav_path = Path(tmp_file.name)
        try:
            cmd = [
                executable,
                "--model",
                self._piper_model_path,
                "--output_file",
                str(wav_path),
            ]
            if self._piper_config_path:
                cmd.extend(["--config", self._piper_config_path])

            rospy.logdebug("Running Piper TTS command: %s", cmd)
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            self._play_audio(wav_path)
        finally:
            wav_path.unlink(missing_ok=True)

    def _play_audio(self, audio_path):
        if not self._audio_player_cmd:
            return
        cmd = _expand_command(self._audio_player_cmd, audio_file=str(audio_path))
        _run_checked(cmd, "audio player")


class OpenAITTS:
    def __init__(self):
        self._client = _make_openai_client()
        self._model = _private_param("openai/tts_model", "gpt-4o-mini-tts")
        self._voice = _private_param("openai/tts_voice", "alloy")
        self._response_format = _private_param("openai/tts_format", "wav")
        self._audio_player_cmd = _command_param(
            "local/audio_player_cmd",
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "{audio_file}"],
        )

    def speak(self, text):
        suffix = "." + self._response_format.lstrip(".")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            audio_path = Path(tmp_file.name)
        try:
            response = self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format=self._response_format,
            )
            _save_openai_audio_response(response, audio_path)
            cmd = _expand_command(self._audio_player_cmd, audio_file=str(audio_path))
            _run_checked(cmd, "audio player")
        finally:
            audio_path.unlink(missing_ok=True)


def _make_openai_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI backend requires the openai Python package in the conda env") from exc

    api_key_env = _private_param("openai/api_key_env", "OPENAI_API_KEY")
    base_url_env = _private_param("openai/base_url_env", "OPENAI_BASE_URL")
    api_key = os.environ.get(api_key_env, "")
    base_url = _private_param("openai/base_url", "") or os.environ.get(base_url_env, "")

    if not api_key:
        raise RuntimeError("{} is required for the OpenAI backend".format(api_key_env))

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _save_openai_audio_response(response, audio_path):
    if hasattr(response, "write_to_file"):
        response.write_to_file(str(audio_path))
        return
    content = getattr(response, "content", None)
    if content is None and hasattr(response, "read"):
        content = response.read()
    if content is None:
        raise RuntimeError("OpenAI speech response did not expose audio bytes")
    audio_path.write_bytes(content)


def _run_checked(cmd, label):
    if not cmd:
        return
    executable = shutil.which(cmd[0]) or cmd[0]
    cmd = [executable] + cmd[1:]
    rospy.logdebug("Running %s command: %s", label, cmd)
    subprocess.run(cmd, check=True)


class VoiceIONode:
    def __init__(self):
        self.sample_rate = int(_private_param("sample_rate", 16000))
        self.channels = int(_private_param("channels", 1))
        self.block_duration_ms = int(_private_param("block_duration_ms", 30))
        self.block_size = max(1, int(self.sample_rate * self.block_duration_ms / 1000.0))
        self.start_threshold = float(_private_param("start_threshold", 0.018))
        self.stop_threshold = float(_private_param("stop_threshold", 0.012))
        self.pre_roll_sec = float(_private_param("pre_roll_sec", 0.35))
        self.end_silence_sec = float(_private_param("end_silence_sec", 0.8))
        self.min_utterance_sec = float(_private_param("min_utterance_sec", 0.35))
        self.max_utterance_sec = float(_private_param("max_utterance_sec", 8.0))
        self.cooldown_sec = float(_private_param("cooldown_sec", 0.4))
        self.publish_empty_result = bool(_private_param("publish_empty_result", False))
        self.pause_listening_while_speaking = bool(_private_param("pause_listening_while_speaking", True))
        self.input_device = _private_param("input_device", "")

        input_text_topic = _private_param("input_text_topic", "/vln/voice_input_text")
        output_text_topic = _private_param("output_text_topic", "/vln/voice_output_text")
        asr_backend = _private_param("asr_backend", "local").lower()
        tts_backend = _private_param("tts_backend", "local").lower()

        self.input_pub = rospy.Publisher(input_text_topic, String, queue_size=10)
        self.output_sub = rospy.Subscriber(output_text_topic, String, self._tts_callback, queue_size=10)
        self.tts_queue = queue.Queue()
        self.tts_active = threading.Event()

        self.asr = self._make_asr(asr_backend)
        self.tts = self._make_tts(tts_backend)

        self.capture_thread = threading.Thread(target=self._capture_loop, name="voice_capture", daemon=True)
        self.tts_thread = threading.Thread(target=self._tts_loop, name="voice_tts", daemon=True)

        rospy.loginfo(
            "voice_io_node ready: ASR=%s TTS=%s input_topic=%s output_topic=%s sample_rate=%d",
            asr_backend,
            tts_backend,
            input_text_topic,
            output_text_topic,
            self.sample_rate,
        )

    def start(self):
        self.tts_thread.start()
        self.capture_thread.start()

    def _make_asr(self, backend):
        if backend == "local":
            return LocalVoskASR(self.sample_rate)
        if backend == "openai":
            return OpenAIASR(self.sample_rate, self.channels)
        raise RuntimeError("unsupported asr_backend: {}".format(backend))

    def _make_tts(self, backend):
        if backend == "local":
            return LocalTTS()
        if backend == "openai":
            return OpenAITTS()
        raise RuntimeError("unsupported tts_backend: {}".format(backend))

    def _tts_callback(self, msg):
        text = msg.data.strip()
        if not text:
            return
        self.tts_queue.put(text)

    def _tts_loop(self):
        while not rospy.is_shutdown():
            try:
                text = self.tts_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.tts_active.set()
                rospy.loginfo("Speaking TTS text: %s", text)
                self.tts.speak(text)
            except Exception as exc:
                rospy.logerr("TTS failed: %s", exc)
            finally:
                time.sleep(self.cooldown_sec)
                self.tts_active.clear()
                self.tts_queue.task_done()

    def _capture_loop(self):
        try:
            import sounddevice as sd
        except ImportError as exc:
            rospy.logfatal("sounddevice is required in the conda env: %s", exc)
            rospy.signal_shutdown("missing sounddevice")
            return

        device = self.input_device if self.input_device not in ("", None) else None
        pre_roll_blocks = max(1, int(self.pre_roll_sec / (self.block_duration_ms / 1000.0)))
        pre_roll = deque(maxlen=pre_roll_blocks)

        try:
            with sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=self.channels,
                dtype="int16",
                device=device,
            ) as stream:
                rospy.loginfo("Listening for speech on input device: %s", device if device is not None else "default")
                self._read_utterances(stream, pre_roll)
        except Exception as exc:
            rospy.logfatal("microphone capture failed: %s", exc)
            rospy.signal_shutdown("microphone capture failed")

    def _read_utterances(self, stream, pre_roll):
        listening = False
        utterance = []
        utterance_start = 0.0
        last_voice_time = 0.0

        while not rospy.is_shutdown():
            data, overflowed = stream.read(self.block_size)
            if overflowed:
                rospy.logwarn_throttle(5.0, "microphone input overflow")

            block = bytes(data)

            if self.pause_listening_while_speaking and self.tts_active.is_set():
                listening = False
                utterance = []
                pre_roll.clear()
                continue

            now = time.monotonic()
            rms = audioop.rms(block, 2) / 32768.0

            if not listening:
                pre_roll.append(block)
                if rms >= self.start_threshold:
                    listening = True
                    utterance_start = now
                    last_voice_time = now
                    utterance = list(pre_roll)
                    rospy.loginfo("Speech started")
                continue

            utterance.append(block)
            if rms >= self.stop_threshold:
                last_voice_time = now

            duration = now - utterance_start
            silence = now - last_voice_time
            if duration >= self.max_utterance_sec or (
                duration >= self.min_utterance_sec and silence >= self.end_silence_sec
            ):
                pcm_bytes = b"".join(utterance)
                listening = False
                utterance = []
                pre_roll.clear()
                self._handle_utterance(pcm_bytes, duration)
                time.sleep(self.cooldown_sec)

    def _handle_utterance(self, pcm_bytes, duration):
        rospy.loginfo("Speech ended; transcribing %.2f seconds of audio", duration)
        try:
            text = self.asr.transcribe(pcm_bytes)
        except Exception as exc:
            rospy.logerr("ASR failed: %s", exc)
            return

        if text or self.publish_empty_result:
            self.input_pub.publish(String(data=text))
            rospy.loginfo("Published ASR text: %s", text if text else "<empty>")
        else:
            rospy.loginfo("ASR returned empty text; nothing published")


def main():
    rospy.init_node("voice_io_node")
    try:
        node = VoiceIONode()
    except Exception as exc:
        rospy.logfatal("voice_io_node initialization failed: %s", exc)
        raise SystemExit(1)
    node.start()
    rospy.spin()


if __name__ == "__main__":
    main()
