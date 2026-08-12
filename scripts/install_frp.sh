#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="0.69.0"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
MACHINE="$(uname -m)"

case "$OS" in
  darwin|linux) ;;
  *)
    echo "不支持的操作系统: $OS" >&2
    exit 1
    ;;
esac

case "$MACHINE" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64) ARCH="amd64" ;;
  *)
    echo "不支持的 CPU 架构: $MACHINE" >&2
    exit 1
    ;;
esac

case "${OS}_${ARCH}" in
  darwin_arm64) SHA256="07663f5fa71330f074b25e32cc8bc4ae5ed40d9c2ee1690cbd981774475997a2" ;;
  darwin_amd64) SHA256="3bb1df7aa716a80ddd0b0f108b4e6487bc1e9dae60b22bb67fff6c890bfcc182" ;;
  linux_arm64) SHA256="24a4fc82b4c041835103419685ea124c4d6a7dbf83d0425481c5831b4ce4b3a4" ;;
  linux_amd64) SHA256="6b90d1cd28fc661f170c0de90dde03d2c63e4fd7ce0ae2da2ca1c28014b8146e" ;;
esac

ARCHIVE="frp_${VERSION}_${OS}_${ARCH}.tar.gz"
URL="https://github.com/fatedier/frp/releases/download/v${VERSION}/${ARCHIVE}"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

echo "下载 frpc v${VERSION} (${OS}/${ARCH})…"
curl --fail --location --retry 3 --output "$TEMP_DIR/$ARCHIVE" "$URL"

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(sha256sum "$TEMP_DIR/$ARCHIVE" | awk '{print $1}')"
else
  ACTUAL_SHA256="$(shasum -a 256 "$TEMP_DIR/$ARCHIVE" | awk '{print $1}')"
fi
if [ "$ACTUAL_SHA256" != "$SHA256" ]; then
  echo "SHA-256 校验失败" >&2
  exit 1
fi

tar -xzf "$TEMP_DIR/$ARCHIVE" -C "$TEMP_DIR"
mkdir -p "$ROOT_DIR/bin"
install -m 0755 "$TEMP_DIR/frp_${VERSION}_${OS}_${ARCH}/frpc" "$ROOT_DIR/bin/frpc"

echo "frpc 已安装到 $ROOT_DIR/bin/frpc"
"$ROOT_DIR/bin/frpc" --version
