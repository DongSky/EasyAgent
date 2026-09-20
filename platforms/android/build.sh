#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
: "${ANDROID_NDK_HOME:?Set ANDROID_NDK_HOME to your Android NDK}"
rustup target add aarch64-linux-android
command -v cargo-ndk >/dev/null || { echo 'Install cargo-ndk with cargo install cargo-ndk --locked'; exit 1; }
cargo ndk -t arm64-v8a -o platforms/android/app/src/main/jniLibs build --manifest-path core/Cargo.toml --release --features android
mkdir -p platforms/android/app/src/main/assets/licenses
cp LICENSE LICENSES/Apache-2.0.txt LICENSING.md NOTICE platforms/android/app/src/main/assets/licenses/
cd platforms/android
gradle assembleDebug
