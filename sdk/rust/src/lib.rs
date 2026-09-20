// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

use reqwest::{Client, Method};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

pub type Error = Box<dyn std::error::Error + Send + Sync>;
mod workflows;
pub use workflows::{Artifact, RunHandle, RunResult, RunStopped, WorkflowHandle};

#[derive(Debug, Serialize, Deserialize)]
pub struct Step {
    pub id: String,
    #[serde(default)]
    pub target: String,
    #[serde(default = "tool_kind")]
    pub kind: String,
    #[serde(default)]
    pub input: serde_json::Map<String, Value>,
    #[serde(default)]
    pub depends_on: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub when: Option<Condition>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub body: Option<Box<Workflow>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workflow_ref: Option<WorkflowReference>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_revision: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compensate: Option<Compensation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_attempts: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_seconds: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub not_before: Option<f64>,
    #[serde(default)]
    pub requires_approval: bool,
}
fn tool_kind() -> String {
    "tool".into()
}

#[derive(Debug, Serialize, Deserialize)]
pub struct WorkflowReference {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<u32>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Workflow {
    pub name: String,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub inputs: serde_json::Map<String, Value>,
    #[serde(default)]
    pub metadata: serde_json::Map<String, Value>,
    #[serde(default)]
    pub limits: RunLimits,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Condition {
    pub source: String,
    pub equals: Value,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Compensation {
    pub target: String,
    #[serde(default)]
    pub input: serde_json::Map<String, Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_revision: Option<u32>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct RunLimits {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_calls: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost_usd: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub child_runs: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wall_time_seconds: Option<f64>,
}

#[derive(Clone)]
pub struct HubClient {
    base: String,
    token: String,
    http: Client,
}

impl HubClient {
    pub fn from_env() -> Result<Self, Error> {
        Self::new(
            &std::env::var("EAH_URL").unwrap_or_else(|_| "http://127.0.0.1:8765".into()),
            &std::env::var("EAH_TOKEN").unwrap_or_default(),
        )
    }
    pub fn workflow(&self, id: &str) -> WorkflowHandle {
        WorkflowHandle::new(self.clone(), id)
    }
    pub fn run_handle(&self, id: &str) -> RunHandle {
        RunHandle::new(self.clone(), id)
    }
    pub async fn upload_file(&self, path: impl AsRef<std::path::Path>) -> Result<Artifact, Error> {
        workflows::upload_file(self, path.as_ref()).await
    }
    pub fn new(base: &str, token: &str) -> Result<Self, Error> {
        Ok(Self {
            base: base.trim_end_matches('/').into(),
            token: token.into(),
            http: Client::builder().timeout(Duration::from_secs(30)).build()?,
        })
    }
    pub async fn request(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        key: Option<&str>,
    ) -> Result<Value, Error> {
        let mut request = self.http.request(method, format!("{}{}", self.base, path));
        if !self.token.is_empty() {
            request = request.bearer_auth(&self.token);
        }
        if let Some(value) = body {
            request = request.json(value);
        }
        if let Some(value) = key {
            request = request.header("Idempotency-Key", value);
        }
        let response = request.send().await?;
        if !response.status().is_success() {
            return Err(
                format!("Hub HTTP {}: {}", response.status(), response.text().await?).into(),
            );
        }
        Ok(response.json().await?)
    }
    pub async fn skills(&self) -> Result<Value, Error> {
        self.request(Method::GET, "/v1/skills", None, None).await
    }
    pub async fn install_skill(
        &self,
        package: &Value,
        expected_revision: u64,
    ) -> Result<Value, Error> {
        self.request(
            Method::POST,
            "/v1/skill-packages/install",
            Some(&serde_json::json!({"package":package,"expected_revision":expected_revision})),
            None,
        )
        .await
    }
    pub async fn create_goal(&self, specification: &Value) -> Result<Value, Error> {
        self.request(Method::POST, "/v1/goals", Some(specification), None)
            .await
    }
    pub async fn goal(&self, id: &str) -> Result<Value, Error> {
        self.request(
            Method::GET,
            &format!("/v1/goals/{}", Self::safe_id(id)?),
            None,
            None,
        )
        .await
    }
    pub async fn control_goal(
        &self,
        id: &str,
        action: &str,
        feedback: &str,
    ) -> Result<Value, Error> {
        self.request(
            Method::POST,
            &format!("/v1/goals/{}/control", Self::safe_id(id)?),
            Some(&serde_json::json!({"action":action,"feedback":feedback})),
            None,
        )
        .await
    }
    pub async fn extensions(&self) -> Result<Value, Error> {
        self.request(Method::GET, "/v1/extensions", None, None)
            .await
    }
    pub async fn install_extension(
        &self,
        package: &Value,
        grants: &[String],
        trust_digest: Option<&str>,
        preview: bool,
    ) -> Result<Value, Error> {
        self.request(
            Method::POST,
            if preview {
                "/v1/extensions/preview"
            } else {
                "/v1/extensions/install"
            },
            Some(&json!({"package":package,"grants":grants,"trust_digest":trust_digest})),
            None,
        )
        .await
    }
    pub async fn create_conversation(
        &self,
        model: &str,
        title: &str,
        agent: &Value,
    ) -> Result<Value, Error> {
        self.request(
            Method::POST,
            "/v1/conversations",
            Some(&json!({"model":model,"title":title,"agent":agent})),
            None,
        )
        .await
    }
    pub async fn extension_command(
        &self,
        name: &str,
        input: &Value,
        conversation_id: Option<&str>,
    ) -> Result<Value, Error> {
        self.request(
            Method::POST,
            &format!("/v1/extensions/commands/{}", Self::safe_id(name)?),
            Some(&json!({"input":input,"conversation_id":conversation_id})),
            None,
        )
        .await
    }
    pub async fn send_message(
        &self,
        id: &str,
        text: &str,
        mode: &str,
        key: &str,
    ) -> Result<Value, Error> {
        self.request(
            Method::POST,
            &format!("/v1/conversations/{}/messages", Self::safe_id(id)?),
            Some(&json!({"text":text,"mode":mode,"idempotency_key":key})),
            None,
        )
        .await
    }
    pub async fn library(&self) -> Result<Value, Error> {
        self.request(Method::GET, "/v1/library", None, None).await
    }
    pub async fn export_workflow(&self, workflow: &Value) -> Result<Value, Error> {
        self.request(
            Method::POST,
            "/v1/workflow-packages/export",
            Some(&json!({"workflow": workflow})),
            None,
        )
        .await
    }
    pub async fn import_workflow(
        &self,
        package: &Value,
        bindings: &Value,
        preview: bool,
    ) -> Result<Value, Error> {
        let action = if preview { "preview" } else { "import" };
        let mut body = bindings
            .as_object()
            .ok_or("bindings must be an object")?
            .clone();
        body.insert("package".into(), package.clone());
        self.request(
            Method::POST,
            &format!("/v1/workflow-packages/{action}"),
            Some(&Value::Object(body)),
            None,
        )
        .await
    }
    pub async fn export_component(&self, id: &str, revision: Option<u32>) -> Result<Value, Error> {
        if id.is_empty()
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
        {
            return Err("invalid component id".into());
        }
        let suffix = revision
            .map(|r| format!("?revision={r}"))
            .unwrap_or_default();
        self.request(
            Method::GET,
            &format!("/v1/library/{id}/package{suffix}"),
            None,
            None,
        )
        .await
    }
    pub async fn import_component(
        &self,
        package: &Value,
        bindings: &Value,
        preview: bool,
    ) -> Result<Value, Error> {
        let action = if preview { "preview" } else { "import" };
        self.request(
            Method::POST,
            &format!("/v1/library/packages/{action}"),
            Some(&json!({"package": package, "credential_bindings": bindings})),
            None,
        )
        .await
    }
    pub async fn instantiate(&self, id: &str, options: &Value) -> Result<Value, Error> {
        if id.is_empty()
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
        {
            return Err("invalid component id".into());
        }
        self.request(
            Method::POST,
            &format!("/v1/library/{id}/instantiate"),
            Some(options),
            None,
        )
        .await
    }
    pub async fn submit(&self, workflow: &Value, key: Option<&str>) -> Result<Value, Error> {
        self.request(Method::POST, "/v1/runs", Some(workflow), key)
            .await
    }
    pub async fn submit_typed(
        &self,
        workflow: &Workflow,
        key: Option<&str>,
    ) -> Result<Value, Error> {
        self.submit(&serde_json::to_value(workflow)?, key).await
    }
    fn safe_id(id: &str) -> Result<&str, Error> {
        if id.is_empty()
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            return Err("invalid resource id".into());
        }
        Ok(id)
    }
    pub async fn run(&self, id: &str) -> Result<Value, Error> {
        self.request(
            Method::GET,
            &format!("/v1/runs/{}", Self::safe_id(id)?),
            None,
            None,
        )
        .await
    }
    pub async fn cancel(&self, id: &str) -> Result<Value, Error> {
        self.request(
            Method::POST,
            &format!("/v1/runs/{}/cancel", Self::safe_id(id)?),
            None,
            None,
        )
        .await
    }
    pub async fn approve(&self, id: &str, approved: bool) -> Result<Value, Error> {
        self.request(
            Method::POST,
            &format!("/v1/approvals/{}", Self::safe_id(id)?),
            Some(&json!({"approved": approved})),
            None,
        )
        .await
    }
    pub async fn respond(&self, id: &str, values: &Value) -> Result<Value, Error> {
        self.request(
            Method::POST,
            &format!("/v1/inputs/{}", Self::safe_id(id)?),
            Some(values),
            None,
        )
        .await
    }
    pub async fn events(&self, id: &str, after: u64) -> Result<Value, Error> {
        self.request(
            Method::GET,
            &format!("/v1/runs/{}/events?after={}", Self::safe_id(id)?, after),
            None,
            None,
        )
        .await
    }
    pub async fn wait(&self, id: &str, timeout: Duration) -> Result<Value, Error> {
        let start = Instant::now();
        while start.elapsed() < timeout {
            let run = self.run(id).await?;
            if matches!(
                run["status"].as_str(),
                Some(
                    "succeeded"
                        | "failed"
                        | "cancelled"
                        | "waiting_approval"
                        | "waiting_input"
                        | "needs_attention"
                )
            ) {
                return Ok(run);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        Err("timed out waiting for run".into())
    }
}

// Portable package metadata. These contracts do not launch a runtime or grant permissions.
#[derive(Debug, Serialize, Deserialize)]
pub struct RuntimeRequirements {
    pub platforms: Vec<String>,
    pub capabilities: Vec<String>,
    pub network_origins: Vec<String>,
    pub credential_refs: Vec<String>,
    pub memory_mb: u32,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct RuntimeProfile {
    pub id: String,
    pub platform: String,
    pub capabilities: Vec<String>,
    pub network_origins: Vec<String>,
    pub credential_refs: Vec<String>,
    pub memory_mb: u32,
    pub location: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ComponentReference {
    pub kind: String,
    pub id: String,
    pub revision: u32,
    pub digest: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ComponentManifest {
    pub schema_version: u32,
    pub id: String,
    pub revision: u32,
    pub title: String,
    pub description: String,
    pub source: ComponentReference,
    pub dependencies: Vec<ComponentReference>,
    pub input_schema: Value,
    pub output_schema: Value,
    pub defaults: Value,
    pub requirements: RuntimeRequirements,
    pub effect: String,
    pub docs: Vec<String>,
    pub validation: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct CodePackage {
    pub id: String,
    pub revision: u32,
    pub language: String,
    pub entrypoint: String,
    pub source_digests: std::collections::BTreeMap<String, String>,
    pub dependency_lock_digest: String,
    pub requirements: RuntimeRequirements,
    pub integration_scenarios: Vec<String>,
    pub state: String,
}

/// Implement the language-neutral extension protocol in a native trusted process.
pub fn serve_extension(
    handler: impl Fn(&str, &Value, &Value) -> Result<Value, Error>,
) -> Result<(), Error> {
    use std::io::BufRead;
    let mut line = String::new();
    std::io::stdin().lock().read_line(&mut line)?;
    if line.len() > 2_000_000 {
        return Err("request too large".into());
    }
    let request: Value = serde_json::from_str(&line)?;
    if request["protocol_version"] != 1 {
        return Err("unsupported protocol".into());
    }
    let method = request["method"].as_str().ok_or("missing method")?;
    let result = handler(method, &request["params"], &request["context"])?;
    println!("{}", json!({"result":result}));
    Ok(())
}
