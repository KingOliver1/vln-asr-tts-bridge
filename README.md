# asr4trailer

ROS1 voice input/output bridge for VLN navigation. The node listens to the laptop microphone, publishes recognized text, and speaks text received from ROS.

## Topics

- Publishes `std_msgs/String` on `/vln/voice_input_text`
- Subscribes `std_msgs/String` on `/vln/voice_output_text`

Both topic names can be changed by launch arguments or ROS remaps.

## Conda environment

Dependencies are isolated in a dedicated conda environment:

```bash
./scripts/create_conda_env.sh
```

The environment intentionally does not install `rospy`. The launcher sources ROS Noetic so conda Python imports `rospy` from `/opt/ros/noetic/lib/python3/dist-packages`.
The environment creation script and launcher both set `PYTHONNOUSERSITE=1` so the node does not import Python packages from `~/.local`.

Check the environment:

```bash
conda run -n asr4trailer_voice python -c "import sounddevice, vosk, openai; print('voice deps ok')"
```

## Local backend

Local ASR uses Vosk. Set `local/vosk_model_path` in `config/voice_io.yaml` to a Chinese Vosk model directory, for example `models/vosk-model-small-cn-0.22`.

Local TTS uses Piper when `local/piper_model_path` is set and a `piper` executable is available. Piper is treated as an optional external command because its phonemizer wheels are not always available from the configured pip mirror. If no Piper model is configured, the node falls back to `spd-say`.

## OpenAI-compatible backend

Set:

```bash
export OPENAI_API_KEY=...
```

Optionally set:

```bash
export OPENAI_BASE_URL=...
```

Launch with:

```bash
./scripts/run_voice_io_conda.sh asr_backend:=openai tts_backend:=openai
```

## Run

From this package directory:

```bash
./scripts/run_voice_io_conda.sh
```

Publish speech output text:

```bash
rostopic pub -1 /vln/voice_output_text std_msgs/String "data: '已经到达目的地啦'"
```

Echo recognized input text:

```bash
rostopic echo /vln/voice_input_text
```
