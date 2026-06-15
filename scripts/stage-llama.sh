#!/bin/bash
# Stage the prebuilt llama.cpp llama-bench workload into the tree.
#
# FetchContent-style: the binary + .so closure are NOT committed (they are
# large external build artifacts -- see .gitignore: workloads/llama/,
# inputs/*.gguf). This script downloads the prebuilt llama.cpp release from
# GitHub and the GGUF model from HuggingFace, caches them under .cache/, and
# stages them into the tree. run-vm.sh calls it automatically when the llama
# workload is missing.
#
# We always pull the ubuntu-x64 release: the .so closure runs inside the x64
# buildroot guest, not on whatever host invokes this script. The downloaded
# release does NOT bundle libgomp/libssl/libcrypto, and the guest rootfs lacks
# them, so those three are still copied from the build host.
#
# Usage:
#   scripts/stage-llama.sh                 # download (cached) + stage
#   LLAMA_VERSION=b9568 scripts/stage-llama.sh
#   LLAMA_CACHE=/path/to/cache scripts/stage-llama.sh
#   LLAMA_MODEL=/path/to/model.gguf scripts/stage-llama.sh   # use a local model
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO="$(dirname "$SCRIPT_DIR")"

# --- Knobs ------------------------------------------------------------------
LLAMA_VERSION="${LLAMA_VERSION:-b9568}"
LLAMA_CACHE="${LLAMA_CACHE:-$REPO/.cache/llama}"
ASSET="llama-${LLAMA_VERSION}-bin-ubuntu-x64.tar.gz"
RELEASE_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${ASSET}"

MODEL_NAME="qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/${MODEL_NAME}?download=true"

DEST="$REPO/workloads/llama"
INPUTS="$REPO/inputs"
mkdir -p "$DEST" "$INPUTS" "$LLAMA_CACHE"

# --- Downloader (curl or wget), with a clear error if neither is present ----
fetch() {  # fetch <url> <dest>
    local url="$1" dst="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 -o "$dst" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$dst" "$url"
    else
        echo "!! need curl or wget to download $url" >&2
        return 1
    fi
}

echo "Version      : $LLAMA_VERSION (ubuntu-x64)"
echo "Cache        : $LLAMA_CACHE"
echo "Binaries+libs: $DEST"
echo "Model        : $INPUTS"
echo

# --- Download + unpack the prebuilt release (cached) ------------------------
# The ubuntu-x64 tarball unpacks to a flat top-level dir, llama-<version>/,
# holding llama-bench, llama-cli and the lib*.so* closure.
TARBALL="$LLAMA_CACHE/$ASSET"
LLAMA_DIR="$LLAMA_CACHE/llama-${LLAMA_VERSION}"
if [ ! -e "$LLAMA_DIR/llama-bench" ]; then
    if [ ! -e "$TARBALL" ]; then
        echo "  download $ASSET"
        fetch "$RELEASE_URL" "$TARBALL.part"
        mv "$TARBALL.part" "$TARBALL"
    else
        echo "  cached   $ASSET"
    fi
    echo "  unpack   $ASSET"
    tar xzf "$TARBALL" -C "$LLAMA_CACHE"
fi
[ -e "$LLAMA_DIR/llama-bench" ] || { echo "!! $LLAMA_DIR/llama-bench missing after unpack" >&2; exit 1; }

# --- Binaries: llama-bench (driver) + llama-cli (optional correctness smoke) -
for bin in llama-bench llama-cli; do
    if [ -e "$LLAMA_DIR/$bin" ]; then
        cp -a "$LLAMA_DIR/$bin" "$DEST/"
        echo "  bin  $bin"
    fi
done

# --- llama/ggml shared libs (preserve the soname symlink chains) ------------
# Globs cover the real files and their soname symlinks (e.g. libggml.so ->
# libggml.so.0 -> libggml.so.0.14.0). cp -a keeps the chain intact, and the
# libggml-cpu-*.so variants ggml dlopen()s at runtime come along for free.
shopt -s nullglob
for g in "$LLAMA_DIR"/libllama*.so* "$LLAMA_DIR"/libggml*.so* "$LLAMA_DIR"/libmtmd*.so*; do
    cp -a "$g" "$DEST/"
done
echo "  libs $(ls "$DEST"/lib*.so* 2>/dev/null | wc -l) llama/ggml .so files"

# --- Host libs the guest rootfs lacks (resolve real path, copy under soname) -
copy_host_lib() {
    local soname="$1" src
    # Try the standard multiarch dir, then ask the dynamic loader.
    for src in "/usr/lib/x86_64-linux-gnu/$soname" "/lib/x86_64-linux-gnu/$soname"; do
        [ -e "$src" ] && { cp -aL "$src" "$DEST/$soname"; echo "  host $soname"; return 0; }
    done
    src="$(ldconfig -p 2>/dev/null | awk -v s="$soname" '$1==s {print $NF; exit}')"
    if [ -n "$src" ] && [ -e "$src" ]; then
        cp -aL "$src" "$DEST/$soname"; echo "  host $soname"; return 0
    fi
    echo "  !! could not find host $soname -- guest run may fail to load" >&2
    return 1
}
copy_host_lib libgomp.so.1   || true
copy_host_lib libssl.so.3    || true
copy_host_lib libcrypto.so.3 || true

# --- Model into inputs/ (download from HF, or copy a local override) --------
DST_MODEL="$INPUTS/$MODEL_NAME"
if [ -n "${LLAMA_MODEL:-}" ]; then
    # Explicit local model override.
    if [ ! -e "$LLAMA_MODEL" ]; then
        echo "  !! LLAMA_MODEL not found: $LLAMA_MODEL" >&2
    else
        dst="$INPUTS/$(basename "$LLAMA_MODEL")"
        if [ -e "$dst" ] && [ "$(stat -c%s "$dst")" = "$(stat -c%s "$LLAMA_MODEL")" ]; then
            echo "  model $(basename "$LLAMA_MODEL") already staged (same size, skipped)"
        else
            cp -a "$LLAMA_MODEL" "$dst"
            echo "  model $(basename "$LLAMA_MODEL")"
        fi
    fi
elif [ -e "$DST_MODEL" ]; then
    echo "  model $MODEL_NAME already present (skipped)"
else
    echo "  download $MODEL_NAME"
    fetch "$MODEL_URL" "$DST_MODEL.part"
    mv "$DST_MODEL.part" "$DST_MODEL"
    echo "  model $MODEL_NAME"
fi

echo
echo "Staged. Total size:"
du -sh "$DEST" 2>/dev/null | sed 's/^/  workloads\/llama  /'
du -sh "$INPUTS"/*.gguf 2>/dev/null | sed 's/^/  /' || true
echo
echo "Sanity-check the closure on the host with:"
echo "  LD_LIBRARY_PATH=$DEST ldd $DEST/llama-bench   # expect no 'not found'"
