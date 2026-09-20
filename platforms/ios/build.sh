#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
rustup target add aarch64-apple-ios-sim
cargo build --release --manifest-path core/Cargo.toml --target aarch64-apple-ios-sim
mkdir -p .eah/build/ios/EasyAgent.app
mkdir -p .eah/build/ios/EasyAgent.app/Licenses
cp LICENSE LICENSES/Apache-2.0.txt LICENSING.md NOTICE .eah/build/ios/EasyAgent.app/Licenses/
cp platforms/ios/Info.plist .eah/build/ios/EasyAgent.app/
xcrun --sdk iphonesimulator swiftc -parse-as-library -target arm64-apple-ios17.0-simulator -sdk "$(xcrun --sdk iphonesimulator --show-sdk-path)" -import-objc-header platforms/ios/Core.h platforms/ios/Main.swift core/target/aarch64-apple-ios-sim/release/libeasyagent_core.a -framework UIKit -framework WebKit -o .eah/build/ios/EasyAgent.app/EasyAgent
codesign --force --sign - .eah/build/ios/EasyAgent.app
