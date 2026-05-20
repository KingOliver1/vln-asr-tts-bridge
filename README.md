# asr4trailer

`asr4trailer` 是一个面向 VLN 导航的 ROS1 语音输入/输出桥接包。节点会监听本机笔记本麦克风，把识别到的语音文本发布到 ROS 话题；同时订阅 ROS 话题中的待播报文本，并通过本机扬声器播放。

## 话题

- 发布 `std_msgs/String` 到 `/vln/voice_input_text`
- 订阅 `std_msgs/String` 从 `/vln/voice_output_text`

两个话题名都可以通过 launch 参数或 ROS remap 修改。

## Conda 环境

所有 Python 依赖都隔离在独立 conda 环境 `asr4trailer_voice` 中：

```bash
./scripts/create_conda_env.sh
```

该环境不会安装 `rospy`。启动脚本会 source ROS Noetic，使 conda Python 从 `/opt/ros/noetic/lib/python3/dist-packages` 导入 `rospy`。

环境创建脚本和启动脚本都会设置 `PYTHONNOUSERSITE=1`，避免节点导入 `~/.local` 中的用户级 Python 包。

检查环境：

```bash
conda run -n asr4trailer_voice python -c "import sounddevice, vosk, openai; print('voice deps ok')"
```

## 本地后端

本地语音识别使用 Vosk。需要在 `config/voice_io.yaml` 中把 `local/vosk_model_path` 设置为中文 Vosk 模型目录，例如：

```yaml
local:
  vosk_model_path: models/vosk-model-small-cn-0.22
```

本地语音合成在配置 `local/piper_model_path` 且系统中存在 `piper` 可执行文件时使用 Piper。Piper 被当作可选外部命令处理，因为它的 phonemizer wheel 在部分 pip 镜像中不可用。

如果没有配置 Piper 模型，节点会回退到 `spd-say`。

## 准备的模型

云端后端已适配阿里云 Model Studio / DashScope：

- ASR：`fun-asr-mtl`
- TTS：`qwen3-tts-vd-2026-01-26`

使用前设置 DashScope API key：

```bash
export DASHSCOPE_API_KEY=你的_api_key
```

启动云端语音识别和语音合成：

```bash
./scripts/run_voice_io_conda.sh asr_backend:=dashscope tts_backend:=dashscope
```

`fun-asr-mtl` 识别本机麦克风录音时，节点会把临时 wav 上传到 DashScope 临时 OSS，再提交识别任务，识别文本发布到 `/vln/voice_input_text`。

`qwen3-tts-vd-2026-01-26` 需要一个 Voice Design 声音。可以在 `config/voice_io.yaml` 中设置 `dashscope/tts_voice` 为已经创建好的 `voice_id`；如果留空，节点启动时会用 `dashscope/tts_voice_prompt` 自动创建一个声音并用于播报。

## OpenAI 兼容后端

设置 API key：

```bash
export OPENAI_API_KEY=...
```

如需使用自定义 OpenAI 兼容服务地址，可选设置：

```bash
export OPENAI_BASE_URL=...
```

使用 OpenAI 后端启动：

```bash
./scripts/run_voice_io_conda.sh asr_backend:=openai tts_backend:=openai
```

## 运行

在本包目录下运行：

```bash
./scripts/run_voice_io_conda.sh
```

发布待播报文本：

```bash
rostopic pub -1 /vln/voice_output_text std_msgs/String "data: '已经到达目的地啦'"
```

查看识别到的语音输入文本：

```bash
rostopic echo /vln/voice_input_text
```

## 麦克风输入排查

当前默认使用 PulseAudio 数字麦克风源采集：

```yaml
audio_capture_backend: parec
input_device: alsa_input.pci-0000_06_00.6.HiFi__hw_acp__source
```

查看系统输入源：

```bash
pactl list short sources
```

如果换了电脑或声卡名称不同，把 `config/voice_io.yaml` 里的 `input_device` 改成 `pactl list short sources` 中对应的信源名称。若想回到 `sounddevice` 直连 ALSA，把 `audio_capture_backend` 改成 `sounddevice`，并用下面命令查设备编号：

```bash
conda run -n asr4trailer_voice python -c "import sounddevice as sd; print(sd.query_devices())"
```

测试麦克风、VAD 阈值和本地 Vosk 识别：

```bash
conda run -n asr4trailer_voice python scripts/test_microphone_input.py --list-devices
```

脚本会录制几秒音频，保存到 `/tmp/asr4trailer_mic_test.wav`，并输出电平、当前阈值是否会触发、以及 Vosk 对测试录音的识别结果。
