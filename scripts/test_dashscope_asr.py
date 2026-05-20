#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import wave
from http import HTTPStatus
from pathlib import Path

import requests
import yaml


PKG_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PKG_DIR / "config" / "voice_io.yaml"
DEFAULT_AUDIO = Path("/tmp/asr4trailer_mic_test.wav")


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


def summarize_wav(path):
    with wave.open(str(path), "rb") as wav_file:
        return {
            "channels": wav_file.getnchannels(),
            "sample_width": wav_file.getsampwidth(),
            "sample_rate": wav_file.getframerate(),
            "frames": wav_file.getnframes(),
            "duration": wav_file.getnframes() / float(wav_file.getframerate()),
        }


def upload_audio(audio_path, model, api_key):
    from dashscope.utils.oss_utils import OssUtils

    print("1. 上传音频到 DashScope 临时 OSS...")
    file_url, _ = OssUtils.upload(model=model, file_path=str(audio_path), api_key=api_key)
    if not file_url:
        raise RuntimeError("上传成功但 file_url 为空")
    print("   上传成功: {}".format(file_url))
    return file_url


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
            result_url = results[0].get("transcription_url")
            if not result_url:
                raise RuntimeError("任务结果缺少 transcription_url: {}".format(data))
            return result_url
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError("任务结束但失败: {}".format(json.dumps(data, ensure_ascii=False)))
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
    audio_path = Path(args.audio)

    print("配置文件: {}".format(args.config))
    print("API key 环境变量: {}={}".format(api_key_env, mask_key(api_key)))
    print("base_url: {}".format(base_url))
    print("ASR model: {}".format(model))
    print("audio: {}".format(audio_path))

    if not api_key:
        raise RuntimeError("未设置 {}。先运行: export {}=你的key".format(api_key_env, api_key_env))
    if not audio_path.is_file():
        raise RuntimeError("音频文件不存在: {}。可以先运行 scripts/test_microphone_input.py 生成。".format(audio_path))

    try:
        info = summarize_wav(audio_path)
        print("wav 信息: {channels}ch, {sample_rate}Hz, sample_width={sample_width}, duration={duration:.2f}s".format(**info))
    except wave.Error:
        print("警告: 无法按 wav 读取该文件，将仍尝试上传。")

    if args.dry_run:
        print("dry-run 完成，没有发起云端请求。")
        return

    file_url = upload_audio(audio_path, model, api_key)
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
