// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0
use easyagent_client::{Error, HubClient};
use serde_json::json;
use std::path::PathBuf;
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let mut args = std::env::args().skip(1);
    let reference = args
        .next()
        .ok_or("Usage: cargo run --example media -- reference.png [output-directory]")?;
    let output = PathBuf::from(
        args.next()
            .unwrap_or_else(|| "output/media-sdk/rust".into()),
    );
    let client = HubClient::from_env()?;
    let key = std::env::var("EAH_DEMO_KEY").unwrap_or_else(|_| "media-demo".into());
    let uploaded = client.upload_file(reference).await?;
    let result = client
        .workflow("media.expression_video")
        .timeout(Duration::from_secs(1900))
        .run(&json!({"reference_image":uploaded.id}), Some(&key))
        .await?;
    result
        .download("image", output.join("expression.png"))
        .await?;
    result
        .download("video", output.join("animation.mp4"))
        .await?;
    println!(
        "{}",
        json!({"language":"Rust","run_id":result.id,"output":output})
    );
    Ok(())
}
