// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

//! Execute a saved component reference through the typed SDK.
use easyagent_client::{Error, HubClient, Workflow};
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let path = std::env::var("EAH_WORKFLOW_FILE")?;
    let workflow: Workflow = serde_json::from_str(&std::fs::read_to_string(path)?)?;
    let client = HubClient::new(
        &std::env::var("EAH_URL")?,
        &std::env::var("EAH_TOKEN").unwrap_or_default(),
    )?;
    let created = client.submit_typed(&workflow, None).await?;
    let run = client
        .wait(
            created["id"].as_str().ok_or("missing run id")?,
            Duration::from_secs(30),
        )
        .await?;
    if run["status"] != "succeeded" {
        return Err(run.to_string().into());
    }
    println!("{}", run);
    Ok(())
}
