// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0
use crate::{Error, HubClient};
use reqwest::{Client, Method, Url};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const LIMIT: usize = 50_000_000;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Artifact {
    pub id: String,
    pub name: String,
    pub media_type: String,
    pub digest: String,
    pub size: u64,
}

#[derive(Debug)]
pub struct RunStopped {
    pub run_id: String,
    pub status: String,
    pub state: Value,
}
impl std::fmt::Display for RunStopped {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Run {}: {}. Inspect state, supply approval/input, then resume this run ID.",
            self.run_id, self.status
        )
    }
}
impl std::error::Error for RunStopped {}

pub struct WorkflowHandle {
    client: HubClient,
    pub id: String,
    revision: Option<u32>,
    timeout: Duration,
}
impl WorkflowHandle {
    pub(crate) fn new(client: HubClient, id: &str) -> Self {
        Self {
            client,
            id: id.into(),
            revision: None,
            timeout: Duration::from_secs(600),
        }
    }
    pub fn revision(mut self, revision: u32) -> Self {
        self.revision = Some(revision);
        self
    }
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }
    /// Submit and wait, retaining the same approvals, key and result contracts.
    pub async fn run(&self, inputs: &Value, key: Option<&str>) -> Result<RunResult, Error> {
        self.start(inputs, key).await?.result_with_timeout(self.timeout).await
    }
    fn path(&self) -> Result<String, Error> {
        if self.id.is_empty()
            || !self
                .id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
        {
            return Err("invalid workflow id".into());
        }
        Ok(format!("/v1/workflows/{}", self.id))
    }
    pub async fn start(&self, inputs: &Value, key: Option<&str>) -> Result<RunHandle, Error> {
        let created = self
            .client
            .request(
                Method::POST,
                &format!("{}/runs", self.path()?),
                Some(&json!({"inputs":inputs,"revision":self.revision})),
                key,
            )
            .await?;
        Ok(RunHandle::new(
            self.client.clone(),
            created["id"].as_str().ok_or("missing run id")?,
        ))
    }
    pub async fn definition(&self) -> Result<Value, Error> {
        let suffix = self
            .revision
            .map(|r| format!("?revision={r}"))
            .unwrap_or_default();
        self.client
            .request(
                Method::GET,
                &format!("{}{suffix}", self.path()?),
                None,
                None,
            )
            .await
    }
}

pub struct RunHandle {
    client: HubClient,
    pub id: String,
}
impl RunHandle {
    pub(crate) fn new(client: HubClient, id: &str) -> Self {
        Self {
            client,
            id: id.into(),
        }
    }
    pub async fn cancel(&self) -> Result<Value, Error> {
        self.client.cancel(&self.id).await
    }
    pub async fn result(&self) -> Result<RunResult, Error> {
        self.result_with_timeout(Duration::from_secs(600)).await
    }
    pub async fn result_with_timeout(&self, timeout: Duration) -> Result<RunResult, Error> {
        if timeout.is_zero() {
            return Err("timeout must be positive".into());
        }
        let path = format!("/v1/runs/{}/result", HubClient::safe_id(&self.id)?);
        let wait = async {
            loop {
                let state = self.client.request(Method::GET, &path, None, None).await?;
                let status = state["status"].as_str().ok_or("missing run status")?;
                if status == "succeeded" {
                    return Ok(RunResult {
                        client: self.client.clone(),
                        id: self.id.clone(),
                        outputs: state["outputs"].clone(),
                        artifacts: serde_json::from_value(state["artifacts"].clone())?,
                        state,
                    });
                }
                if matches!(
                    status,
                    "failed"
                        | "cancelled"
                        | "waiting_approval"
                        | "waiting_input"
                        | "needs_attention"
                ) {
                    return Err(Box::new(RunStopped {
                        run_id: self.id.clone(),
                        status: status.into(),
                        state,
                    }) as Error);
                }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        };
        tokio::time::timeout(timeout, wait).await.map_err(|_| -> Error {
            format!("Run {} timed out locally; resume it instead of resubmitting. The run was not cancelled.", self.id).into()
        })?
    }
}

pub struct RunResult {
    client: HubClient,
    pub id: String,
    pub outputs: Value,
    pub artifacts: Vec<Artifact>,
    pub state: Value,
}
impl RunResult {
    pub async fn download(
        &self,
        output: &str,
        destination: impl AsRef<Path>,
    ) -> Result<PathBuf, Error> {
        let value = self
            .outputs
            .get(output)
            .ok_or("unknown named workflow output")?;
        let mut authenticated = false;
        let url = if let (Some(id), Some(_)) = (value["id"].as_str(), value["digest"].as_str()) {
            authenticated = true;
            Url::parse(&format!(
                "{}/v1/artifacts/{}/content",
                self.client.base,
                HubClient::safe_id(id)?
            ))?
        } else if let Some(url) = value.as_str() {
            let url = Url::parse(url)?;
            if !matches!(url.scheme(), "http" | "https")
                || url.host_str().is_none()
                || !url.username().is_empty()
                || url.password().is_some()
                || url.fragment().is_some()
                || (url.scheme() == "http"
                    && !matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]")))
            {
                return Err(
                    "media URL must use HTTPS (or loopback HTTP), without embedded credentials"
                        .into(),
                );
            }
            url
        } else {
            return Err("named output must be an artifact descriptor or a media URL".into());
        };
        let http = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(120))
            .build()?;
        let mut request = http.get(url);
        if authenticated && !self.client.token.is_empty() {
            request = request.bearer_auth(&self.client.token);
        }
        let mut response = request.send().await?;
        if !response.status().is_success() {
            return Err(format!("Media download HTTP {}", response.status()).into());
        }
        let destination = destination.as_ref();
        let parent = destination
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        tokio::fs::create_dir_all(parent).await?;
        let name = destination
            .file_name()
            .ok_or("destination needs a filename")?
            .to_string_lossy();
        let temporary = parent.join(format!(
            "{name}.{}.{}.part",
            std::process::id(),
            SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()
        ));
        let result = async {
            let mut file = tokio::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary)
                .await?;
            let mut size = 0;
            while let Some(bytes) = response.chunk().await? {
                size += bytes.len();
                if size > LIMIT {
                    return Err("download exceeds 50 MB limit".into());
                }
                file.write_all(&bytes).await?;
            }
            file.flush().await?;
            drop(file);
            tokio::fs::rename(&temporary, destination).await?;
            Ok(destination.to_path_buf())
        }
        .await;
        if result.is_err() {
            let _ = tokio::fs::remove_file(&temporary).await;
        }
        result
    }
}

pub(crate) async fn upload_file(client: &HubClient, path: &Path) -> Result<Artifact, Error> {
    let mut content = Vec::new();
    tokio::fs::File::open(path)
        .await?
        .take((LIMIT + 1) as u64)
        .read_to_end(&mut content)
        .await?;
    if content.len() > LIMIT {
        return Err("file exceeds 50 MB upload limit".into());
    }
    let name = path
        .file_name()
        .ok_or("upload needs a filename")?
        .to_str()
        .ok_or("filename must be UTF-8")?;
    let extension = path
        .extension()
        .and_then(|v| v.to_str())
        .unwrap_or("")
        .to_lowercase();
    let mime = match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "mp4" => "video/mp4",
        "wav" => "audio/wav",
        "mp3" => "audio/mpeg",
        "pdf" => "application/pdf",
        "txt" => "text/plain",
        _ => "application/octet-stream",
    };
    let mut request = client
        .http
        .post(format!("{}/v1/artifacts/upload", client.base))
        .query(&[("name", name)])
        .header("Content-Type", mime)
        .body(content);
    if !client.token.is_empty() {
        request = request.bearer_auth(&client.token);
    }
    Ok(request.send().await?.error_for_status()?.json().await?)
}
