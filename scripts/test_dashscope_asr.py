#!/usr/bin/env python3
import argparse
import mimetypes
import os
import sys
import time
import wave
from array import array
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from time import mktime
from wsgiref.handlers import format_date_time

import requests
import yaml


PKG_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PKG_DIR / "config" / "voice_io.yaml"
DEFAULT_AUDIO = Path("/tmp/asr4trailer_mic_test.wav")
SAMPLE_AUDIO_URL = "https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/paraformer/hello_world_female2.wav"


def load_config(path):
    with open(path, "r", encoding="utf-8") as cfg_file:
        return yaml.safe_load(cfg_file)


def mask_key(value):
    if not value:
        return "<未设置>"
    if len(value) <= 8:
        return "***"
    return "{}...{}".format(value[:4], value[-4:])


def response_json(response, action):
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("{} 返回非 JSON: status={} body={}".format(action, response.status_code, response.text[:500])) from exc

    if response.status_code != HTTPStatus.OK:
        raise RuntimeError(
            "{} 失败: status={} code={} message={}".format(
                action,
                response.status_code,
                data.get("code", ""),
                data.get("message", data),
            )
        )
    return data


def result_summary(results):
    parts = []
    for index, result in enumerate(results or []):
        fields = ["result[{}]".format(index)]
        for key in ("subtask_status", "code", "message"):
            if result.get(key):
                fields.append("{}={}".format(key, result[key]))
        parts.append(" ".join(fields))
    return "; ".join(parts) if parts else "<no results>"


def summarize_wav(path):
    with wave.open(str(path), "rb") as wav_file:
        return {
            "channels": wav_file.getnchannels(),
            "sample_width": wav_file.getsampwidth(),
            "sample_rate": wav_file.getframerate(),
            "frames": wav_file.getnframes(),
            "duration": wav_file.getnframes() / float(wav_file.getframerate()),
        }


def remove_pcm_dc_offset(pcm_bytes):
    if not pcm_bytes:
        return pcm_bytes

    samples = array("h")
    samples.frombytes(pcm_bytes)
    if not samples:
        return pcm_bytes
    if sys.byteorder != "little":
        samples.byteswap()

    offset = int(round(sum(samples) / float(len(samples))))
    if offset == 0:
        return pcm_bytes

    for index, sample in enumerate(samples):
        value = sample - offset
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        samples[index] = value

    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def prepare_audio_for_upload(audio_path, remove_dc_offset):
    if not remove_dc_offset:
        return audio_path, None

    try:
        with wave.open(str(audio_path), "rb") as src:
            params = src.getparams()
            if src.getsampwidth() != 2:
                return audio_path, None
            pcm_bytes = src.readframes(src.getnframes())
    except wave.Error:
        return audio_path, None

    cleaned_path = audio_path.with_name(audio_path.stem + "_dc_removed.wav")
    with wave.open(str(cleaned_path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(remove_pcm_dc_offset(pcm_bytes))
    return cleaned_path, cleaned_path


def upload_audio(audio_path, model, api_key, upload_timeout_sec, http_timeout_sec):
    import requests

    from dashscope.utils.oss_utils import OssUtils
    from dashscope.common.utils import get_user_agent

    print("1. 上传音频到 DashScope 临时 OSS...")
    upload_info = OssUtils.get_upload_certificate(
        model=model,
        api_key=api_key,
        request_timeout=http_timeout_sec,
    )
    if upload_info.status_code != HTTPStatus.OK:
        raise RuntimeError(
            "获取上传凭证失败: code={} message={}".format(
                getattr(upload_info, "code", ""),
                getattr(upload_info, "message", ""),
            )
        )
    upload_info = upload_info.output

    form_data = {
        "OSSAccessKeyId": upload_info["oss_access_key_id"],
        "Signature": upload_info["signature"],
        "policy": upload_info["policy"],
        "key": upload_info["upload_dir"] + "/" + os.path.basename(str(audio_path)),
        "x-oss-object-acl": upload_info["x_oss_object_acl"],
        "x-oss-forbid-overwrite": upload_info["x_oss_forbid_overwrite"],
        "success_action_status": "200",
        "x-oss-content-type": mimetypes.guess_type(str(audio_path))[0],
    }
    headers = {
        "user-agent": get_user_agent(),
        "Accept": "application/json",
        "Date": format_date_time(mktime(datetime.now().timetuple())),
    }
    try:
        with audio_path.open("rb") as audio_file:
            response = requests.post(
                upload_info["upload_host"],
                files={"file": audio_file},
                data=form_data,
                headers=headers,
                timeout=upload_timeout_sec,
            )
    except requests.Timeout as exc:
        raise RuntimeError("上传临时 OSS 超时: {}s".format(upload_timeout_sec)) from exc

    if response.status_code != HTTPStatus.OK:
        raise RuntimeError("上传临时 OSS 失败: status={} body={}".format(response.status_code, response.text[:500]))
    print("   上传成功，已获得 file_url")
    return "oss://" + form_data["key"]


def resolve_input_url(audio_path, sample_audio, model, api_key, remove_dc_offset, upload_timeout_sec, http_timeout_sec):
    if sample_audio:
        print("1. 使用 DashScope 公开样例音频，不上传本地录音:")
        print("   {}".format(SAMPLE_AUDIO_URL))
        return SAMPLE_AUDIO_URL
    upload_path, temporary_path = prepare_audio_for_upload(audio_path, remove_dc_offset)
    try:
        if temporary_path:
            print("   已生成去 DC 临时 wav: {}".format(temporary_path))
        return upload_audio(upload_path, model, api_key, upload_timeout_sec, http_timeout_sec)
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def submit_task(base_url, model, file_url, api_key, language_hints, channel_id):
    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
        "X-DashScope-OssResourceResolve": "enable",
    }
    payload = {
        "model": model,
        "input": {"file_urls": [file_url]},
    }
    parameters = {}
    if language_hints:
        parameters["language_hints"] = language_hints
    if channel_id not in ("", None):
        parameters["channel_id"] = channel_id
    if parameters:
        payload["parameters"] = parameters

    url = base_url.rstrip("/") + "/services/audio/asr/transcription"
    print("2. 提交 Fun-ASR 任务: {}".format(url))
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    data = response_json(response, "提交 ASR 任务")
    task_id = (data.get("output") or {}).get("task_id")
    if not task_id:
        raise RuntimeError("提交成功但没有 task_id: {}".format(data))
    print("   task_id: {}".format(task_id))
    return task_id


def poll_task(base_url, task_id, api_key, interval_sec, timeout_sec):
    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/tasks/{}".format(task_id)
    deadline = time.monotonic() + timeout_sec
    print("3. 轮询任务状态: {}".format(url))
    while time.monotonic() < deadline:
        response = requests.post(url, headers=headers, timeout=30)
        data = response_json(response, "轮询 ASR 任务")
        output = data.get("output") or {}
        status = output.get("task_status", "")
        print("   status: {}".format(status))
        if status == "SUCCEEDED":
            results = output.get("results") or []
            if not results:
                raise RuntimeError("任务成功但 results 为空: {}".format(data))
            for result in results:
                if result.get("subtask_status", "SUCCEEDED") != "SUCCEEDED":
                    continue
                result_url = result.get("transcription_url")
                if result_url:
                    return result_url
            raise RuntimeError("任务成功但没有可用 transcription_url: {}".format(result_summary(results)))
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(
                "任务结束但失败: status={} code={} message={} results={}".format(
                    status,
                    output.get("code", ""),
                    output.get("message", ""),
                    result_summary(output.get("results") or []),
                )
            )
        time.sleep(interval_sec)
    raise RuntimeError("轮询超时: {}s".format(timeout_sec))


def extract_text(data):
    parts = []
    for item in data.get("transcripts", []) or []:
        if item.get("text"):
            parts.append(item["text"].strip())
            continue
        for sentence in item.get("sentences", []) or []:
            if sentence.get("text"):
                parts.append(sentence["text"].strip())
    if not parts:
        for sentence in data.get("sentences", []) or []:
            if sentence.get("text"):
                parts.append(sentence["text"].strip())
    if not parts and data.get("text"):
        parts.append(data["text"].strip())
    return " ".join(part for part in parts if part).strip()


def download_result(result_url):
    print("4. 下载识别结果...")
    response = requests.get(result_url, timeout=30)
    data = response_json(response, "下载识别结果")
    text = extract_text(data)
    print("   识别文本: {}".format(text if text else "<空>"))
    return text


def main():
    parser = argparse.ArgumentParser(description="诊断 DashScope/Fun-ASR 云端识别调用链路")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="voice_io.yaml 路径")
    parser.add_argument("--audio", default=str(DEFAULT_AUDIO), help="用于上传识别的 wav 文件")
    parser.add_argument("--timeout", type=float, default=None, help="覆盖 ASR 轮询超时时间")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置和音频文件，不发起云端请求")
    parser.add_argument("--sample-audio", action="store_true", help="使用 DashScope 公开样例音频测试云端链路")
    args = parser.parse_args()

    cfg = load_config(args.config)
    dashscope_cfg = cfg.get("dashscope", {})
    api_key_env = dashscope_cfg.get("api_key_env", "DASHSCOPE_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    base_url = dashscope_cfg.get("base_http_api_url", "https://dashscope.aliyuncs.com/api/v1")
    model = dashscope_cfg.get("asr_model", "fun-asr-mtl")
    language_hints = dashscope_cfg.get("asr_language_hints", [])
    channel_id = dashscope_cfg.get("asr_channel_id", [0])
    interval_sec = float(dashscope_cfg.get("asr_poll_interval_sec", 1.0))
    timeout_sec = float(args.timeout or dashscope_cfg.get("asr_timeout_sec", 120.0))
    upload_timeout_sec = float(dashscope_cfg.get("asr_upload_timeout_sec", 20.0))
    http_timeout_sec = float(dashscope_cfg.get("asr_http_timeout_sec", 30.0))
    remove_dc_offset = bool(cfg.get("cloud_asr_remove_dc_offset", True))
    audio_path = Path(args.audio)

    print("配置文件: {}".format(args.config))
    print("API key 环境变量: {}={}".format(api_key_env, mask_key(api_key)))
    print("base_url: {}".format(base_url))
    print("ASR model: {}".format(model))
    print("audio: {}".format(SAMPLE_AUDIO_URL if args.sample_audio else audio_path))

    if not api_key:
        raise RuntimeError("未设置 {}。先运行: export {}=你的key".format(api_key_env, api_key_env))
    if not args.sample_audio and not audio_path.is_file():
        raise RuntimeError("音频文件不存在: {}。可以先运行 scripts/test_microphone_input.py 生成。".format(audio_path))

    if not args.sample_audio:
        try:
            info = summarize_wav(audio_path)
            print("wav 信息: {channels}ch, {sample_rate}Hz, sample_width={sample_width}, duration={duration:.2f}s".format(**info))
            print("上传前去 DC: {}".format(remove_dc_offset))
        except wave.Error:
            print("警告: 无法按 wav 读取该文件，将仍尝试上传。")

    if args.dry_run:
        print("dry-run 完成，没有发起云端请求。")
        return

    file_url = resolve_input_url(
        audio_path,
        args.sample_audio,
        model,
        api_key,
        remove_dc_offset,
        upload_timeout_sec,
        http_timeout_sec,
    )
    task_id = submit_task(base_url, model, file_url, api_key, language_hints, channel_id)
    result_url = poll_task(base_url, task_id, api_key, interval_sec, timeout_sec)
    download_result(result_url)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print("DashScope ASR 测试失败: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)
