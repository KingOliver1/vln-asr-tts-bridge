#!/usr/bin/env python3
import argparse
import audioop
import json
import shutil
import statistics
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path

import yaml


PKG_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PKG_DIR / "config" / "voice_io.yaml"
DEFAULT_OUTPUT = Path("/tmp/vln_asr_tts_bridge_mic_test.wav")


def resolve_package_path(value):
    if value in ("", None):
        return ""
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return str(path)
    return str(PKG_DIR / path)


def load_config(path):
    with open(path, "r", encoding="utf-8") as cfg_file:
        return yaml.safe_load(cfg_file)


def write_wav(path, pcm_bytes, sample_rate, channels):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)


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


def capture_with_parec(cfg, duration_sec):
    executable = cfg.get("parec", {}).get("executable", "parec")
    executable = shutil.which(executable) or executable
    sample_rate = int(cfg.get("sample_rate", 48000))
    channels = int(cfg.get("channels", 1))
    device = cfg.get("input_device", "")
    block_ms = int(cfg.get("block_duration_ms", 30))
    block_size = max(1, int(sample_rate * block_ms / 1000.0))
    bytes_per_block = block_size * channels * 2
    total_bytes = int(sample_rate * duration_sec) * channels * 2

    cmd = [
        executable,
        "--record",
        "--format=s16le",
        "--rate={}".format(sample_rate),
        "--channels={}".format(channels),
        "--latency={}".format(bytes_per_block * 2),
    ]
    if device:
        cmd.append("--device={}".format(device))

    print("采集命令: {}".format(" ".join(cmd)))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks = []
    remaining = total_bytes
    try:
        while remaining > 0:
            chunk = process.stdout.read(min(bytes_per_block, remaining))
            if not chunk:
                stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError("parec 没有继续输出音频: {}".format(stderr))
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    return b"".join(chunks)


def capture_with_sounddevice(cfg, duration_sec):
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("sounddevice 未安装在当前 Python 环境中") from exc

    sample_rate = int(cfg.get("sample_rate", 48000))
    channels = int(cfg.get("channels", 1))
    device = cfg.get("input_device", "") or None
    block_ms = int(cfg.get("block_duration_ms", 30))
    block_size = max(1, int(sample_rate * block_ms / 1000.0))
    total_blocks = max(1, int(duration_sec * 1000 / block_ms))
    chunks = []

    print("sounddevice 默认设备: {}".format(sd.default.device))
    print("采集设备: {}".format(device if device is not None else "default"))
    with sd.RawInputStream(
        samplerate=sample_rate,
        blocksize=block_size,
        channels=channels,
        dtype="int16",
        device=device,
    ) as stream:
        for _ in range(total_blocks):
            data, overflowed = stream.read(block_size)
            if overflowed:
                print("警告: sounddevice 输入 overflow")
            chunks.append(bytes(data))

    return b"".join(chunks)


def analyze_audio(pcm_bytes, cfg):
    sample_rate = int(cfg.get("sample_rate", 48000))
    channels = int(cfg.get("channels", 1))
    block_ms = int(cfg.get("block_duration_ms", 30))
    block_size = max(1, int(sample_rate * block_ms / 1000.0)) * channels * 2
    start_threshold = float(cfg.get("start_threshold", 0.018))
    stop_threshold = float(cfg.get("stop_threshold", 0.012))
    start_trigger_blocks = max(1, int(cfg.get("start_trigger_blocks", 1)))
    vad_remove_dc_offset = bool(cfg.get("vad_remove_dc_offset", True))

    blocks = [
        pcm_bytes[index : index + block_size]
        for index in range(0, len(pcm_bytes) - block_size + 1, block_size)
    ]
    rms_values = [
        audioop.rms(remove_pcm_dc_offset(block) if vad_remove_dc_offset else block, 2) / 32768.0
        for block in blocks
        if block
    ]
    if not rms_values:
        raise RuntimeError("录音数据为空，无法分析")

    dc_offset = audioop.avg(pcm_bytes, 2) / 32768.0
    dc_removed_pcm_bytes = remove_pcm_dc_offset(pcm_bytes)
    consecutive = 0
    vad_triggered = False
    for rms in rms_values:
        if rms >= start_threshold:
            consecutive += 1
            if consecutive >= start_trigger_blocks:
                vad_triggered = True
                break
        else:
            consecutive = 0

    sorted_rms = sorted(rms_values)
    p50 = sorted_rms[int(len(sorted_rms) * 0.50)]
    p95 = sorted_rms[max(0, int(len(sorted_rms) * 0.95) - 1)]
    peak_rms = max(rms_values)
    peak_sample = audioop.max(pcm_bytes, 2) / 32768.0
    above_start = sum(1 for value in rms_values if value >= start_threshold)
    above_stop = sum(1 for value in rms_values if value >= stop_threshold)

    return {
        "duration_sec": len(pcm_bytes) / float(sample_rate * channels * 2),
        "blocks": len(rms_values),
        "rms_min": min(rms_values),
        "rms_avg": statistics.mean(rms_values),
        "rms_p50": p50,
        "rms_p95": p95,
        "rms_peak": peak_rms,
        "sample_peak": peak_sample,
        "above_start_blocks": above_start,
        "above_stop_blocks": above_stop,
        "start_threshold": start_threshold,
        "stop_threshold": stop_threshold,
        "start_trigger_blocks": start_trigger_blocks,
        "vad_remove_dc_offset": vad_remove_dc_offset,
        "dc_offset": dc_offset,
        "dc_removed_rms": audioop.rms(dc_removed_pcm_bytes, 2) / 32768.0,
        "dc_removed_sample_peak": audioop.max(dc_removed_pcm_bytes, 2) / 32768.0,
        "vad_triggered": vad_triggered,
    }


def transcribe_with_vosk(pcm_bytes, cfg):
    model_path = resolve_package_path(cfg.get("local", {}).get("vosk_model_path", ""))
    if not model_path:
        return None, "未配置 local/vosk_model_path"
    if not Path(model_path).is_dir():
        return None, "Vosk 模型目录不存在: {}".format(model_path)

    try:
        import vosk
    except ImportError:
        return None, "当前 Python 环境没有安装 vosk"

    sample_rate = int(cfg.get("sample_rate", 48000))
    model = vosk.Model(model_path)
    recognizer = vosk.KaldiRecognizer(model, sample_rate)
    recognizer.AcceptWaveform(pcm_bytes)
    result = json.loads(recognizer.FinalResult())
    return result.get("text", "").strip(), None


def print_sources():
    print("\nPulseAudio 输入源:")
    try:
        subprocess.run(["pactl", "list", "short", "sources"], check=False)
    except FileNotFoundError:
        print("pactl 不存在")

    print("\nsounddevice 设备:")
    try:
        import sounddevice as sd

        print("默认设备: {}".format(sd.default.device))
        print(sd.query_devices())
    except Exception as exc:
        print("无法查询 sounddevice 设备: {}".format(exc))


def print_analysis(analysis):
    print("\n录音分析:")
    print("  时长: {duration_sec:.2f}s, blocks: {blocks}".format(**analysis))
    print(
        "  RMS min/avg/p50/p95/peak: "
        "{rms_min:.5f} / {rms_avg:.5f} / {rms_p50:.5f} / {rms_p95:.5f} / {rms_peak:.5f}".format(
            **analysis
        )
    )
    print("  样本峰值: {sample_peak:.5f}".format(**analysis))
    print(
        "  DC offset: {dc_offset:.5f}, 去 DC 后 RMS/峰值: {dc_removed_rms:.5f} / {dc_removed_sample_peak:.5f}".format(
            **analysis
        )
    )
    print(
        "  VAD 阈值: start={start_threshold:.5f}, stop={stop_threshold:.5f}, 连续触发块={start_trigger_blocks}, 去 DC={vad_remove_dc_offset}".format(
            **analysis
        )
    )
    print(
        "  超过 start/stop 阈值的块数: {above_start_blocks} / {above_stop_blocks}".format(
            **analysis
        )
    )
    print("  当前 VAD 是否会触发: {}".format("是" if analysis["vad_triggered"] else "否"))


def print_conclusion(analysis, vosk_text):
    print("\n判断:")
    if analysis["rms_peak"] < 0.01 and analysis["sample_peak"] < 0.03:
        print("  录音几乎没有有效信号，优先检查麦克风设备、系统输入源、静音和权限。")
        return
    if abs(analysis["dc_offset"]) > 0.02:
        print("  录音存在明显直流偏置；节点会先去 DC 再做云端 ASR/VAD。")
    if not analysis["vad_triggered"] and vosk_text:
        print("  麦克风和本地 Vosk 都可用；问题是能量 VAD 阈值不适合这路输入。")
        print("  当前节点默认启用 local/vosk_streaming，让 Vosk 自己做端点检测，不再依赖 RMS 阈值。")
        return
    if not analysis["vad_triggered"]:
        print("  麦克风录到了声音，但当前 VAD 阈值没有触发。")
        print("  可以降低 config/voice_io.yaml 里的 start_threshold/stop_threshold，或靠近麦克风重试。")
        return
    if vosk_text == "":
        print("  VAD 会触发，麦克风也有信号，但 Vosk 没识别出文字。")
        print("  这更像是音频质量、噪声、说话内容或本地 ASR 模型的问题。")
        return
    if vosk_text:
        print("  麦克风、VAD 和本地 Vosk 识别都可用。若 ROS 节点仍无话题输出，再查节点运行日志和话题。")
        return
    print("  麦克风和 VAD 结果已输出；本次没有运行 Vosk 识别。")


def main():
    parser = argparse.ArgumentParser(description="测试 VLN ASR/TTS bridge 麦克风采集、VAD 阈值和本地 Vosk 识别")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="voice_io.yaml 路径")
    parser.add_argument("--duration", type=float, default=6.0, help="录音时长，秒")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="保存测试 wav 的路径")
    parser.add_argument("--backend", choices=("parec", "sounddevice"), default=None, help="覆盖配置中的采集后端")
    parser.add_argument("--device", default=None, help="覆盖配置中的输入设备/信源")
    parser.add_argument("--no-vosk", action="store_true", help="只测试录音和 VAD，不运行 Vosk 识别")
    parser.add_argument("--list-devices", action="store_true", help="同时列出 PulseAudio 和 sounddevice 设备")
    parser.add_argument("--countdown", type=int, default=2, help="录音前倒计时秒数")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.backend:
        cfg["audio_capture_backend"] = args.backend
    if args.device is not None:
        cfg["input_device"] = args.device

    backend = cfg.get("audio_capture_backend", "sounddevice")
    print("配置文件: {}".format(args.config))
    print("采集后端: {}".format(backend))
    print("输入设备: {}".format(cfg.get("input_device", "") or "default"))
    print("采样率/通道: {} Hz / {} ch".format(cfg.get("sample_rate", 48000), cfg.get("channels", 1)))
    print("测试录音会保存到: {}".format(args.output))

    if args.list_devices:
        print_sources()

    if args.countdown > 0:
        print("\n请在录音期间说一句中文，例如：我要去实验室。")
        for remaining in range(args.countdown, 0, -1):
            print("{}...".format(remaining), flush=True)
            time.sleep(1)

    print("开始录音 {:.1f}s".format(args.duration))
    if backend == "parec":
        pcm_bytes = capture_with_parec(cfg, args.duration)
    elif backend == "sounddevice":
        pcm_bytes = capture_with_sounddevice(cfg, args.duration)
    else:
        raise RuntimeError("不支持的 audio_capture_backend: {}".format(backend))
    print("录音结束，读取 {} bytes".format(len(pcm_bytes)))

    output_path = Path(args.output)
    write_wav(output_path, pcm_bytes, int(cfg.get("sample_rate", 48000)), int(cfg.get("channels", 1)))
    print("已保存 wav: {}".format(output_path))

    analysis = analyze_audio(pcm_bytes, cfg)
    print_analysis(analysis)

    vosk_text = None
    if not args.no_vosk:
        print("\n正在用本地 Vosk 模型识别测试录音...")
        vosk_text, vosk_error = transcribe_with_vosk(pcm_bytes, cfg)
        if vosk_error:
            print("Vosk 未运行: {}".format(vosk_error))
        else:
            print("Vosk 识别结果: {}".format(vosk_text if vosk_text else "<空>"))

    print_conclusion(analysis, vosk_text)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print("测试失败: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)
