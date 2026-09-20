// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

use easyagent_client::{Error, HubClient, Workflow};
use serde_json::json;
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let client = HubClient::new(
        &std::env::var("EAH_URL").unwrap_or("http://127.0.0.1:8765".into()),
        &std::env::var("EAH_TOKEN").unwrap_or_default(),
    )?;
    let created = client
        .submit(
            &json!({"name":"Rust SDK demo","steps":[
                {"id":"hello","target":"core.echo","input":{"language":"Rust","message":"hello"}}
            ]}),
            None,
        )
        .await?;
    let run = client
        .wait(
            created["id"].as_str().ok_or("missing run id")?,
            Duration::from_secs(30),
        )
        .await?;
    if run["status"] != "succeeded" {
        return Err(run.to_string().into());
    }
    let workflow: Workflow = serde_json::from_value(json!({
        "name": "Rust typed human input", "inputs": {"source":"Rust"},
        "metadata": {"synthetic":true}, "limits": {"tool_calls":0},
        "steps": [
            {"id":"question","kind":"input","input":{"schema":{"type":"object","properties":{"date":{"type":"string"}},"required":["date"]}}},
            {"id":"answer","kind":"transform","depends_on":["question"],"input":{"date":{"$ref":"question.date"},"source":{"$ref":"$input.source"}}}
        ]
    }))?;
    let created_input = client.submit_typed(&workflow, None).await?;
    let input_id = created_input["id"].as_str().ok_or("missing input run id")?;
    let paused = client.wait(input_id, Duration::from_secs(30)).await?;
    if paused["status"] != "waiting_input" {
        return Err("expected human input pause".into());
    }
    client
        .respond(
            paused["input_requests"][0]["id"]
                .as_str()
                .ok_or("missing question")?,
            &json!({"date":"2026-10-26"}),
        )
        .await?;
    let resumed = client.wait(input_id, Duration::from_secs(30)).await?;
    if resumed["status"] != "succeeded"
        || resumed["steps"][1]["output"]["source"] != "Rust"
        || resumed["steps"][1]["output"]["date"] != "2026-10-26"
    {
        return Err("typed input resume failed".into());
    }
    println!(
        "{}",
        json!({"language":"Rust","id":run["id"],"output":run["steps"][0]["output"]})
    );
    Ok(())
}
