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
