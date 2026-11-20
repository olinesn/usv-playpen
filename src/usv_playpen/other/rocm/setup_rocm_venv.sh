#!/usr/bin/env bash
# Build a virtual environment for usv-playpen on Linux with an AMD GPU (ROCm).
#
# On Linux, uv.lock pins PyTorch to its CUDA 12.8 build. That build runs on
# NVIDIA GPUs only; on an AMD machine every PyTorch step falls back to the CPU.
# This script installs every package at exactly the version uv.lock pins, with
# two differences:
#
#   * torch and torchvision come from PyTorch's ROCm index, at the locked
#     versions (2.11.0 -> 2.11.0+rocm7.2), with the triton-rocm they require;
#   * SAM 2 is built without its optional CUDA extension (SAM2_BUILD_CUDA=0), as
#     on any machine without the CUDA compiler.
#
# PyTorch's ROCm wheels carry their own ROCm libraries and cover the AMD GPUs
# PyTorch builds for (Instinct MI200/MI300, Radeon RX 6000/7000/9000, and the
# Ryzen AI Max "Strix Halo" iGPU, gfx1151). Every PyTorch step then runs on the
# GPU: DAS, Vocalocator and the squeak detector run on PyTorch.
#
# JAX stays on the CPU, as it does after `uv sync` (its GPU build is the
# optional `gpu` extra). USV_PLAYPEN_JAX_ROCM=1 also installs JAX's ROCm plugin
# (jax-rocm7-plugin / jax-rocm7-pjrt, at the locked JAX version), which needs
# ROCm 7 under /opt/rocm. It is opt-in because PyTorch and JAX can then share
# the GPU in one process only if `import torch` comes before `import jax`: the
# other order finds no GPU or crashes. With the plugin installed,
# JAX_PLATFORMS=cpu keeps JAX on the CPU.
#
# The lab's main environment (.venv, made by `uv sync`) is not touched. The
# ROCm environment goes to .venv-rocm next to it (or the folder given as the
# first argument). Use it by activating it and running the commands directly:
#
#   bash src/usv_playpen/other/rocm/setup_rocm_venv.sh   # build or update .venv-rocm
#   source .venv-rocm/bin/activate && usv-playpen
#   source .venv-rocm/bin/activate && pytest -n 8
#
# `uv run` and `uv sync` work on .venv instead, so they would not use the ROCm
# environment. Re-run this script after every `git pull` or tag checkout to
# bring .venv-rocm in line with the new uv.lock.
#
# Arguments after the folder are passed to `uv export` to choose dependency
# groups, e.g.
# `bash src/usv_playpen/other/rocm/setup_rocm_venv.sh .venv-rocm --all-groups`
# (default: the `dev` group, as `uv sync`).
#
# Environment:
#   USV_PLAYPEN_ROCM_INDEX   PyTorch ROCm index (default .../whl/rocm7.2)
#   USV_PLAYPEN_PYTHON       Python version for the environment (default 3.13)
#   USV_PLAYPEN_JAX_ROCM     1: also install JAX's ROCm plugin (see above)

set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "ROCm builds of PyTorch and JAX exist for Linux only." >&2
    exit 1
fi
if ! command -v uv > /dev/null 2>&1; then
    echo "uv not found: install it first (https://docs.astral.sh/uv/getting-started/installation/)." >&2
    exit 1
fi

# This script sits in <repo>/src/usv_playpen/other/rocm/.
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
if [[ ! -f "${repo_dir}/pyproject.toml" || ! -f "${repo_dir}/uv.lock" ]]; then
    echo "Run this script from a usv-playpen checkout (no pyproject.toml / uv.lock in ${repo_dir})." >&2
    exit 1
fi
venv_dir="${1:-${repo_dir}/.venv-rocm}"
shift || true
rocm_index="${USV_PLAYPEN_ROCM_INDEX:-https://download.pytorch.org/whl/rocm7.2}"
rocm_tag="${rocm_index%/}"
rocm_tag="${rocm_tag##*/}"
python_version="${USV_PLAYPEN_PYTHON:-3.13}"
jax_rocm="${USV_PLAYPEN_JAX_ROCM:-0}"

work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

# Every locked package except torch, torchvision and their CUDA libraries, which
# are replaced below. SAM 2 is installed on its own (it needs torch to build).
uv export --project "${repo_dir}" --frozen --no-hashes --no-emit-project \
    --format requirements-txt -o "${work_dir}/locked.txt" "$@" > /dev/null
grep -vE '^(torch|torchvision|triton|nvidia-[a-z0-9-]+-cu12|cuda-bindings|cuda-pathfinder|cuda-toolkit|sam-2)( |=|@)' \
    "${work_dir}/locked.txt" > "${work_dir}/base.txt"

torch_version="$(grep -oE '^torch==[0-9.]+\+cu[0-9]+' "${work_dir}/locked.txt" | sed -E 's/^torch==([0-9.]+)\+.*/\1/')"
torchvision_version="$(grep -oE '^torchvision==[0-9.]+\+cu[0-9]+' "${work_dir}/locked.txt" | sed -E 's/^torchvision==([0-9.]+)\+.*/\1/')"
jax_version="$(grep -oE '^jax==[0-9.]+' "${work_dir}/locked.txt" | sed -E 's/^jax==//')"
sam2_line="$(grep -E '^sam-2 @ ' "${work_dir}/locked.txt" | sed -E 's/ ;.*//')"
if [[ -z "${torch_version}" || -z "${torchvision_version}" || -z "${jax_version}" || -z "${sam2_line}" ]]; then
    echo "Could not read the locked torch / torchvision / jax / sam-2 versions from uv.lock." >&2
    exit 1
fi

echo "Environment: ${venv_dir} (Python ${python_version})"
if [[ "${jax_rocm}" == "1" ]]; then
    jax_note="with jax-rocm7-plugin"
else
    jax_note="on the CPU"
fi
echo "torch ${torch_version}+${rocm_tag}, torchvision ${torchvision_version}+${rocm_tag}, jax ${jax_version} ${jax_note}"

if [[ ! -x "${venv_dir}/bin/python" ]]; then
    uv venv --python "${python_version}" "${venv_dir}"
fi
export VIRTUAL_ENV="${venv_dir}"

# 1. Everything uv.lock pins, apart from the packages replaced here. `pip sync`
#    makes the environment match the list exactly, so it also removes packages
#    an earlier run installed that the lock no longer lists (and, each time, the
#    ROCm packages, which steps 2-5 put back from uv's cache).
uv pip sync --python "${venv_dir}/bin/python" "${work_dir}/base.txt"

# 2. PyTorch and torchvision for ROCm, at the locked versions. Their other
#    requirements are already installed at the locked versions and stay.
uv pip install --python "${venv_dir}/bin/python" --index-url "${rocm_index}" \
    "torch==${torch_version}+${rocm_tag}" "torchvision==${torchvision_version}+${rocm_tag}"

# 3. Only with USV_PLAYPEN_JAX_ROCM=1: JAX's ROCm plugin at the locked JAX
#    version (it loads the system ROCm). Without it, step 1 has removed any
#    plugin an earlier run installed.
if [[ "${jax_rocm}" == "1" ]]; then
    uv pip install --python "${venv_dir}/bin/python" \
        "jax-rocm7-plugin==${jax_version}" "jax-rocm7-pjrt==${jax_version}"
fi

# 4. SAM 2 from the locked commit, built against the torch installed above,
#    without the optional CUDA extension (only used to fill holes in masks).
SAM2_BUILD_CUDA=0 uv pip install --python "${venv_dir}/bin/python" \
    --no-build-isolation --no-deps "${sam2_line}"

# 5. usv-playpen itself, editable, without touching its dependencies.
uv pip install --python "${venv_dir}/bin/python" --no-deps -e "${repo_dir}"

# torch is imported first: JAX's ROCm plugin, if installed, needs that order.
"${venv_dir}/bin/python" - << 'EOF'
import torch
import jax

if torch.cuda.is_available():
    print(f"PyTorch {torch.__version__} sees {torch.cuda.get_device_name(0)} (HIP {torch.version.hip})")
else:
    print(f"PyTorch {torch.__version__} sees no GPU: PyTorch steps will run on the CPU")
print(f"JAX {jax.__version__} devices: {jax.devices()}")
EOF
echo "Done. Activate with: source ${venv_dir}/bin/activate"
