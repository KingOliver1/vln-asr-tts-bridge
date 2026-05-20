# vln-asr-tts-bridge

`vln-asr-tts-bridge` 是一个面向 VLN 导航任务的 ROS1 语音桥接包。它把麦克风语音通过 ASR 转成文本发布到 ROS 话题，同时订阅 ROS 话题中的文本并通过 TTS 播放语音。

ROS 包名是 `vln_asr_tts_bridge`，仓库名建议使用 `vln-asr-tts-bridge`。当前已完整测试的云端模型为阿里云 Model Studio / DashScope；OpenAI 兼容后端保留在代码中，但当前主要验证链路是阿里云。

## 功能

- 发布语音识别结果：`std_msgs/String` 到 `/vln/voice_input_text`
- 订阅待播报文本：`std_msgs/String` 从 `/vln/voice_output_text`
- ASR 后端：`local`、`dashscope`、`openai`
- TTS 后端：`local`、`dashscope`、`openai`
- 本地 ASR：Vosk 中文模型
- 本地 TTS：Piper，未配置时回退到 `spd-say`
- 云端 ASR/TTS：目前测试通过阿里云 `fun-asr-mtl` 和 `qwen3-tts-vd-2026-01-26`

## 安装环境

```bash
git clone https://github.com/KingOliver1/vln-asr-tts-bridge.git
cd vln-asr-tts-bridge
./scripts/create_conda_env.sh
```

Conda 环境名默认为 `vln_asr_tts_voice`。如果你还在使用旧环境 `asr4trailer_voice`，可以临时这样启动：

```bash
ASR4TRAILER_CONDA_ENV=asr4trailer_voice ./scripts/run_voice_io_conda.sh
```

检查依赖：

```bash
conda run -n vln_asr_tts_voice python -c "import sounddevice, vosk, openai, dashscope; print('deps ok')"
```

该 conda 环境不安装 `rospy`。启动脚本会 source ROS Noetic，并把本包加入 `ROS_PACKAGE_PATH`。

## 下载本地模型

`models/` 和 `tools/` 不进入 Git，需要按需下载。

Vosk 中文 ASR 模型：

```bash
mkdir -p models
cd models
wget https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip
unzip vosk-model-cn-0.22.zip

# 可选：更小但准确率较低的模型
wget https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
unzip vosk-model-small-cn-0.22.zip
```

默认配置使用：

```yaml
local:
  vosk_model_path: models/vosk-model-cn-0.22
```

Piper 中文 TTS 模型可以从 `rhasspy/piper-voices` 下载 `zh_CN/huayan/medium` 的 `.onnx` 和 `.onnx.json` 文件，放到：

```text
models/piper/zh_CN_huayan_medium/
```

默认配置期望：

```yaml
local:
  piper_executable: tools/piper/piper
  piper_model_path: models/piper/zh_CN_huayan_medium/zh_CN-huayan-medium.onnx
  piper_config_path: models/piper/zh_CN_huayan_medium/zh_CN-huayan-medium.onnx.json
```

如果没有 Piper，节点会使用 `spd-say` 作为本地 TTS 回退方案。

## 本地 ASR/TTS

确认 `config/voice_io.yaml` 里本地模型路径存在后启动：

```bash
./scripts/run_voice_io_conda.sh asr_backend:=local tts_backend:=local
```

查看 ASR 输出：

```bash
rostopic echo /vln/voice_input_text
```

测试 TTS 输入：

```bash
rostopic pub -1 /vln/voice_output_text std_msgs/String "data: '已经到达目的地啦'"
```

## 阿里云 ASR/TTS

目前云端链路只完整测试了阿里云 Model Studio / DashScope：

- ASR：`fun-asr-mtl`
- TTS：`qwen3-tts-vd-2026-01-26`

先设置 API key：

```bash
export DASHSCOPE_API_KEY=你的_api_key
```

启动阿里云 ASR 和 TTS：

```bash
./scripts/run_voice_io_conda.sh asr_backend:=dashscope tts_backend:=dashscope
```

`fun-asr-mtl` 会把本机麦克风录音写成临时 WAV，上传到 DashScope 临时 OSS，再提交异步识别任务。识别结果发布到 `/vln/voice_input_text`。

`qwen3-tts-vd-2026-01-26` 需要 Voice Design 声音。建议在 `config/voice_io.yaml` 里设置已有 `dashscope/tts_voice`；如果留空，节点启动时会根据 `dashscope/tts_voice_prompt` 创建一个声音。

云端 ASR 诊断：

```bash
conda run -n vln_asr_tts_voice python scripts/test_microphone_input.py
conda run -n vln_asr_tts_voice python scripts/test_dashscope_asr.py
```

如果想先排除麦克风问题，用阿里云公开样例音频测试云端链路：

```bash
conda run -n vln_asr_tts_voice python scripts/test_dashscope_asr.py --sample-audio
```

如果公开样例能识别，而本机录音返回 `SUCCESS_WITH_NO_VALID_FRAGMENT`，说明 API key、上传、提交和轮询都是通的，问题通常是录音片段没有有效语音、输入设备不对、说话太短，或 VAD 阈值截断了语音。

## OpenAI 兼容后端

代码保留 OpenAI 兼容 ASR/TTS 后端：

```bash
export OPENAI_API_KEY=你的_api_key
export OPENAI_BASE_URL=可选的兼容服务地址
./scripts/run_voice_io_conda.sh asr_backend:=openai tts_backend:=openai
```

该后端不是当前主要测试链路。

## 麦克风排查

当前默认使用 PulseAudio 的 `parec` 采集：

```yaml
audio_capture_backend: parec
input_device: alsa_input.pci-0000_06_00.6.HiFi__hw_acp__source
```

查看系统输入源：

```bash
pactl list short sources
```

如果换了电脑，把 `config/voice_io.yaml` 里的 `input_device` 改成当前机器的输入源。若要改回 `sounddevice` 直连 ALSA：

```yaml
audio_capture_backend: sounddevice
input_device: ""
```

查询 sounddevice 设备：

```bash
conda run -n vln_asr_tts_voice python -c "import sounddevice as sd; print(sd.query_devices())"
```

测试麦克风、VAD 阈值和 Vosk：

```bash
conda run -n vln_asr_tts_voice python scripts/test_microphone_input.py --list-devices
```

脚本会保存测试录音到 `/tmp/vln_asr_tts_bridge_mic_test.wav`，并输出电平、DC offset、VAD 是否触发，以及 Vosk 对测试录音的识别结果。

## 重要配置

常用参数在 `config/voice_io.yaml`：

```yaml
asr_backend: local
tts_backend: local
sample_rate: 48000
audio_capture_backend: parec
start_threshold: 0.010
stop_threshold: 0.004
start_trigger_blocks: 3
vad_remove_dc_offset: true
cloud_asr_remove_dc_offset: true
```

如果无声环境也频繁触发 `Speech started`，适当提高 `start_threshold`，例如 `0.015` 或 `0.020`。如果说话不触发，降低 `start_threshold` 或检查麦克风输入设备。

## 仓库改名

本地目录建议改为：

```bash
mv Asr4trailer vln-asr-tts-bridge
```

GitHub 仓库建议从 `KingOliver1/Asr4trailer` 改名为：

```text
KingOliver1/vln-asr-tts-bridge
```

改名后更新远端地址：

```bash
git remote set-url origin https://github.com/KingOliver1/vln-asr-tts-bridge.git
```
