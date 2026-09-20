// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

use easyagent_client::{ComponentManifest, HubClient, Workflow};
use serde_json::json;
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), easyagent_client::Error> {
    let base = std::env::var("EAH_URL")?;
    let hub = HubClient::new(&base, "")?;
    let component = hub
        .instantiate(
            "library.json.receipt",
            &json!({"input": {"value": {"sdk":"rust"}, "filename":"rust.json"}}),
        )
        .await?;
    let manifest: ComponentManifest = serde_json::from_value(component["component"].clone())?;
    assert_eq!(serde_json::to_value(manifest)?, component["component"]);
    let package = hub.export_component("library.json.receipt", None).await?;
    let preview = hub.import_component(&package, &json!({}), true).await?;
    assert_eq!(preview["execution_started"], false);
    let workflow: Workflow = serde_json::from_value(
        json!({"name":"Rust component library", "steps":[component["step"]]}),
    )?;
    let portable = hub
        .export_workflow(&serde_json::to_value(&workflow)?)
        .await?;
    let ready = hub.import_workflow(&portable, &json!({}), true).await?;
    assert_eq!(ready["ready"], true);
    let run = hub.submit_typed(&workflow, None).await?;
    let result = hub
        .wait(run["id"].as_str().unwrap(), Duration::from_secs(10))
        .await?;
    println!("{}", result);
    Ok(())
}
