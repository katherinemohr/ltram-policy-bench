#!/bin/bash
# Stage the prebuilt llama.cpp llama-bench workload into the tree.
#
# The llama-bench binary is dynamically linked, so -- like the ycsbc + libtbb
# workload -- we ship the binary together with its .so closure and run it with
# LD_LIBRARY_PATH pointing at that dir inside the guest. The guest (buildroot
# glibc) already has the loader, libstdc++, libgcc_s, libc and libm; it does NOT
# have libgomp/libssl/libcrypto, so we copy those from the build host too.
#
# These artifacts are large and external, so they are gitignored (see
# .gitignore: workloads/llama/, inputs/*.gguf). Re-run this script on any host
# to repopulate them.
#
# Usage:
#   scripts/stage-llama.sh
#   LLAMA_DIR=/path/to/llama-b9568 scripts/stage-llama.sh
#   LLAMA_MODEL=/path/to/model.gguf scripts/stage-llama.sh
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO="$(dirname "$SCRIPT_DIR")"

# --- Locate the prebuilt llama.cpp artifact dir -----------------------------
find_llama_dir() {
    for d in "${LLAMA_DIR:-}" "$REPO/../llama-b9568" "/home-kmohr/workspace/llama-b9568"; do
        [ -n "$d" ] && [ -e "$d/llama-bench" ] && { echo "$d"; return 0; }
    done
    return 1
}
if ! LLAMA_DIR="$(find_llama_dir)"; then
    echo "!! Could not find a llama-b9568 dir containing llama-bench." >&2
    echo "   Set LLAMA_DIR=/path/to/llama-b9568 and re-run." >&2
    exit 1
fi
LLAMA_DIR="$(cd "$LLAMA_DIR" && pwd)"
MODEL="${LLAMA_MODEL:-$LLAMA_DIR/qwen2.5-0.5b-instruct-q4_k_m.gguf}"

DEST="$REPO/workloads/llama"
INPUTS="$REPO/inputs"
mkdir -p "$DEST" "$INPUTS"

echo "Staging from : $LLAMA_DIR"
echo "Binaries+libs: $DEST"
echo "Model        : $INPUTS"
echo

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

# --- Model into inputs/ (idempotent: skip if already present same size) -----
if [ ! -e "$MODEL" ]; then
    echo "  !! model not found: $MODEL (set LLAMA_MODEL=...)" >&2
else
    dst="$INPUTS/$(basename "$MODEL")"
    if [ -e "$dst" ] && [ "$(stat -c%s "$dst")" = "$(stat -c%s "$MODEL")" ]; then
        echo "  model $(basename "$MODEL") already staged (same size, skipped)"
    else
        cp -a "$MODEL" "$dst"
        echo "  model $(basename "$MODEL")"
    fi
fi

echo
echo "Staged. Total size:"
du -sh "$DEST" 2>/dev/null | sed 's/^/  workloads\/llama  /'
du -sh "$INPUTS"/*.gguf 2>/dev/null | sed 's/^/  /' || true
echo
echo "Sanity-check the closure on the host with:"
echo "  LD_LIBRARY_PATH=$DEST ldd $DEST/llama-bench   # expect no 'not found'"
