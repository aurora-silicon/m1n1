#!/bin/bash
# Build the factory (unconfigured, proxy-only) J613 stage 1 and record what it is.
#
#   tools/j613-stage1/build.sh [--tag TAG] [--commit SHA] [--out DIR]
#
# The stage-1 version tag is what stage 2 publishes as
# /chosen/asahi,m1n1-stage1-version and what installers gate on. The default is
# v1.6.1-m3next.stage1, the version iconidentify/aurora-linux's
# --m3-profile=j613-25g83 admits. --tag overrides it (it must match
# [A-Za-z0-9._+-]+, the installer's rule). Traceability lives in manifest.json:
# the source commit, git describe and the commit-derived name
# v1.6.1-j613s1-g<12-hex commit>. From a git tree the commit is HEAD and the
# tree must be clean; from an exported tree (git archive) pass --commit and
# --describe.
#
# Output in DIR (default build/j613-stage1): m1n1-j613-stage1-<tag>.bin, its
# raw ELF, manifest.json and SHA256SUMS. Fill the image for a Mac with
# tools/j613-stage1/fill_stage1_config.py; this script never fills or installs.
#
# Toolchain: RUSTUP_TOOLCHAIN (default 1.89.0) with aarch64-unknown-none-softfloat,
# clang/lld (TOOLCHAIN/LLDDIR, default /usr/bin/). Cargo registry paths are
# remapped so the image does not depend on the builder's home directory.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
tag="" commit="" describe="" out="$root/build/j613-stage1"
while (($#)); do
  case $1 in
    --tag) tag=$2; shift 2 ;;
    --commit) commit=$2; shift 2 ;;
    --describe) describe=$2; shift 2 ;;
    --out) out=$2; shift 2 ;;
    *) echo "usage: $0 [--tag TAG] [--commit SHA] [--describe DESC] [--out DIR]" >&2; exit 2 ;;
  esac
done

cd "$root"
if git rev-parse --git-dir >/dev/null 2>&1; then
  head=$(git rev-parse HEAD)
  [[ -z $commit || $commit == "$head" ]] || { echo "error: --commit $commit is not HEAD $head" >&2; exit 1; }
  commit=$head
  describe=$(git describe --tags --always --long --abbrev=12)
  if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
    echo "error: the tree has uncommitted changes; commit them so the tag names the source" >&2
    exit 1
  fi
fi
[[ $commit =~ ^[0-9a-f]{40}$ ]] || { echo "error: no source commit; pass --commit <40-hex sha>" >&2; exit 1; }
[[ -n $describe ]] || { echo "error: no git describe; pass --describe" >&2; exit 1; }
default_tag="v1.6.1-j613s1-g${commit:0:12}"
tag=${tag:-v1.6.1-m3next.stage1}
[[ $tag =~ ^[A-Za-z0-9._+-]+$ ]] || { echo "error: tag '$tag' must match [A-Za-z0-9._+-]+" >&2; exit 1; }

export RUSTUP_TOOLCHAIN=${RUSTUP_TOOLCHAIN:-1.89.0}
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || echo 0)}
cargo_home=${CARGO_HOME:-$HOME/.cargo}
export RUSTFLAGS="--remap-path-prefix=$cargo_home=/cargo --remap-path-prefix=$root=/m1n1"
TOOLCHAIN=${TOOLCHAIN:-/usr/bin/}
LLDDIR=${LLDDIR:-/usr/bin/}

make clean >/dev/null
M1N1_VERSION_TAG=$tag make ARCH=aarch64-linux-gnu- USE_CLANG=1 TOOLCHAIN="$TOOLCHAIN" LLDDIR="$LLDDIR" \
  RELEASE=1 CHAINLOADING=1 J613_ESP_STAGE1=1 -j"${JOBS:-4}"

mkdir -p "$out"
bin="$out/m1n1-j613-stage1-$tag.bin"
cp build/m1n1.bin "$bin"
cp build/m1n1-raw.elf "$out/m1n1-j613-stage1-$tag.elf" 2>/dev/null || true

# The image must be a factory (proxy-only) J613 stage 1 carrying this tag.
python3 -I "$here/fill_stage1_config.py" --check "$bin" | tee "$out/check.txt"
grep -q "stage-1 version tag: $tag\$" "$out/check.txt"
grep -q "configuration: factory" "$out/check.txt"

# Layout. iBoot places the panel DCP's live __OS_LOG buffer directly after the
# installed raw image (seen at +0x110000 after a 0x110000-byte image on 26.6.2,
# and at +0x120000 on 14.x stubs). Stage 1 chainloads stage 2 to its own base,
# so the image size S is the room stage 2 has before that buffer:
#  - Aurora m1n1 (1.6.1.aurora15) stage 2 copies its file part (0xe0000) above
#    the image and its payload starts at 0x3e0000: needs 0xe0000 <= S and
#    S + 0x24000 (the log) <= 0x3e0000;
#  - the single-file G54/D3 stage 2 runs in place: needs its 0x114000 <= S.
size=$(stat -c %s "$bin" 2>/dev/null || stat -f %z "$bin")
if ((size < 0x114000 || size > 0x3bc000)) && [[ ${J613_STAGE1_SKIP_LAYOUT:-0} != 1 ]]; then
  printf 'error: stage 1 is 0x%x bytes; it must be 0x114000..0x3bc000 (see the layout note)\n' "$size" >&2
  exit 1
fi

tool() { "$@" 2>/dev/null | head -1 || true; }
python3 -I - "$bin" "$tag" "$default_tag" "$commit" "$describe" "$out/manifest.json" \
  "$(tool rustc --version)" "$(tool "${TOOLCHAIN}clang" --version)" "$(tool "${LLDDIR}ld.lld" --version)" <<'PY'
import hashlib, json, sys
from pathlib import Path
binp, tag, default_tag, commit, describe, manifest, rustc, clang, lld = sys.argv[1:]
data = Path(binp).read_bytes()
Path(manifest).write_text(json.dumps(dict(
    artifact=Path(binp).name, bytes=len(data), size_hex=hex(len(data)),
    sha256=hashlib.sha256(data).hexdigest(), md5=hashlib.md5(data).hexdigest(),
    stage1_version_tag=tag, traceable_name=default_tag, source_commit=commit, git_describe=describe,
    build="make RELEASE=1 USE_CLANG=1 CHAINLOADING=1 J613_ESP_STAGE1=1",
    toolchain=dict(rustc=rustc, clang=clang, lld=lld),
    configuration="factory (proxy-only); fill with tools/j613-stage1/fill_stage1_config.py (default window 0)",
    guard="T8122 + ADT target-type J613 + OS firmware 26.6.2, else proxy only"), indent=2) + "\n")
PY
(cd "$out" && sha256sum "$(basename "$bin")" > SHA256SUMS 2>/dev/null || shasum -a 256 "$(basename "$bin")" > SHA256SUMS)
cat "$out/manifest.json"
