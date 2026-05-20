#!/usr/bin/env bash
set -euo pipefail

CONDA_EXE="${CONDA_EXE:-/home/tianbot/miniconda3/bin/conda}"

if [ ! -x "$CONDA_EXE" ]; then
  CONDA_EXE="/home/tianbot/miniconda3/condabin/conda"
fi

if [ ! -x "$CONDA_EXE" ]; then
  echo "conda executable not found. Set CONDA_EXE and retry." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Prevent pip from treating ~/.local packages as satisfied dependencies.
export PYTHONNOUSERSITE=1

exec "$CONDA_EXE" env create -f "${PKG_DIR}/environment.yml"
