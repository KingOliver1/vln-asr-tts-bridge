#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${VLN_ASR_TTS_CONDA_ENV:-${ASR4TRAILER_CONDA_ENV:-vln_asr_tts_voice}}"
LEGACY_ENV_NAME="asr4trailer_voice"
CONDA_EXE="${CONDA_EXE:-/home/tianbot/miniconda3/bin/conda}"

if [ ! -x "$CONDA_EXE" ]; then
  CONDA_EXE="/home/tianbot/miniconda3/condabin/conda"
fi

if [ ! -x "$CONDA_EXE" ]; then
  echo "conda executable not found. Set CONDA_EXE or install the ${ENV_NAME} environment first." >&2
  exit 1
fi

CONDA_BASE="$("$CONDA_EXE" info --base)"
# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"

if ! "$CONDA_EXE" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  if [ "$ENV_NAME" = "vln_asr_tts_voice" ] && "$CONDA_EXE" env list | awk '{print $1}' | grep -qx "$LEGACY_ENV_NAME"; then
    echo "conda env ${ENV_NAME} not found; using legacy env ${LEGACY_ENV_NAME}" >&2
    ENV_NAME="$LEGACY_ENV_NAME"
  fi
fi

conda activate "$ENV_NAME"

# Keep the conda environment isolated from ~/.local Python packages.
export PYTHONNOUSERSITE=1

# Keep rospy from the ROS Noetic installation; do not install it into conda.
# shellcheck disable=SC1091
source /opt/ros/noetic/setup.bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

for setup_file in \
  "${PKG_DIR}/../../devel/setup.bash" \
  "${PKG_DIR}/../devel/setup.bash" \
  "${PKG_DIR}/devel/setup.bash"; do
  if [ -f "$setup_file" ]; then
    # shellcheck disable=SC1090
    source "$setup_file"
    break
  fi
done

# Allows roslaunch to find this package even before it is installed into a catkin workspace.
export ROS_PACKAGE_PATH="${PKG_DIR}:${ROS_PACKAGE_PATH:-}"

exec roslaunch vln_asr_tts_bridge voice_io.launch "$@"
