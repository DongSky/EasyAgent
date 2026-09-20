// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

//! Platform-independent state transitions. The host owns storage and effects.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{CStr, CString};
use std::os::raw::c_char;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub schema_version: u32,
    pub run_id: String,
    pub workflow: Value,
    pub nodes: BTreeMap<String, Node>,
    pub grants: BTreeMap<String, String>,
    pub sequence: u64,
    pub outbox: BTreeMap<String, Value>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub status: String,
    pub output: Value,
    pub error: Option<String>,
    pub attempt: u32,
}
fn error(s: &str) -> String {
    s.into()
}
fn steps(w: &Value) -> Result<&Vec<Value>, String> {
    w["steps"]
        .as_array()
        .ok_or(error("workflow requires steps"))
}
fn id(s: &Value) -> Result<&str, String> {
    s["id"]
        .as_str()
        .filter(|s| !s.is_empty() && !s.contains(':'))
        .ok_or(error("invalid step ID"))
}
fn deps(s: &Value) -> Result<Vec<&str>, String> {
    match s.get("depends_on") {
        None => Ok(vec![]),
        Some(v) => v
            .as_array()
            .ok_or(error("invalid dependencies"))?
            .iter()
            .map(|d| d.as_str().ok_or(error("invalid dependency")))
            .collect(),
    }
}
fn resolve(v: &Value, values: &Value) -> Result<Value, String> {
    match v {
        Value::Object(m) if m.contains_key("$ref") => {
            let path = m["$ref"].as_str().ok_or(error("invalid reference"))?;
            let mut current = values;
            for part in path.split('.') {
                current = if let Value::Array(a) = current {
                    a.get(part.parse::<usize>().map_err(|_| error("invalid index"))?)
                        .ok_or(error("missing index"))?
                } else {
                    current.get(part).ok_or(error("missing reference"))?
                };
            }
            Ok(current.clone())
        }
        Value::Object(m) => Ok(Value::Object(
            m.iter()
                .map(|(k, v)| Ok((k.clone(), resolve(v, values)?)))
                .collect::<Result<_, String>>()?,
        )),
        Value::Array(a) => Ok(Value::Array(
            a.iter()
                .map(|v| resolve(v, values))
                .collect::<Result<_, _>>()?,
        )),
        _ => Ok(v.clone()),
    }
}
impl Snapshot {
    pub fn new(
        run_id: String,
        workflow: Value,
        grants: BTreeMap<String, String>,
    ) -> Result<Self, String> {
        if run_id.is_empty() || run_id.contains(':') {
            return Err(error("invalid run ID"));
        }
        let list = steps(&workflow)?;
        if list.is_empty() || list.len() > 500 {
            return Err(error("workflow needs 1..500 steps"));
        }
        let mut nodes = BTreeMap::new();
        for step in list {
            let name = id(step)?.to_owned();
            if nodes
                .insert(
                    name,
                    Node {
                        status: "queued".into(),
                        output: Value::Null,
                        error: None,
                        attempt: 0,
                    },
                )
                .is_some()
            {
                return Err(error("duplicate step"));
            }
        }
        let mut done = BTreeSet::new();
        while done.len() < list.len() {
            let before = done.len();
            for step in list {
                if deps(step)?.iter().all(|d| done.contains(*d)) {
                    done.insert(id(step)?.to_owned());
                }
            }
            if done.len() == before {
                return Err(error("cyclic or missing dependency"));
            }
        }
        Ok(Self {
            schema_version: 1,
            run_id,
            workflow,
            nodes,
            grants,
            sequence: 0,
            outbox: BTreeMap::new(),
        })
    }
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1 {
            return Err(error("unsupported state schema"));
        }
        let expected = Self::new(
            self.run_id.clone(),
            self.workflow.clone(),
            self.grants.clone(),
        )?;
        if self.nodes.keys().ne(expected.nodes.keys()) {
            return Err(error("checkpoint node set differs from workflow"));
        }
        if self.nodes.values().any(|n| {
            !matches!(
                n.status.as_str(),
                "queued"
                    | "running"
                    | "waiting_approval"
                    | "succeeded"
                    | "failed"
                    | "cancelled"
                    | "skipped"
                    | "needs_attention"
            )
        }) {
            return Err(error("invalid checkpoint status"));
        }
        Ok(())
    }
    pub fn next_actions(&mut self) -> Result<Vec<Value>, String> {
        self.validate()?;
        let mut values = serde_json::Map::new();
        values.insert(
            "$input".into(),
            self.workflow.get("inputs").cloned().unwrap_or(json!({})),
        );
        for (id, node) in &self.nodes {
            if node.status == "succeeded" {
                values.insert(id.clone(), node.output.clone());
            }
        }
        let values = Value::Object(values);
        let mut actions = vec![];
        for step in steps(&self.workflow)? {
            let name = id(step)?;
            if self.nodes[name].status != "queued" {
                continue;
            }
            let dependencies = deps(step)?;
            if dependencies.iter().any(|d| {
                matches!(
                    self.nodes[*d].status.as_str(),
                    "failed" | "cancelled" | "skipped"
                )
            }) {
                self.nodes.get_mut(name).unwrap().status = "skipped".into();
                continue;
            }
            if !dependencies
                .iter()
                .all(|d| self.nodes[*d].status == "succeeded" && values.get(*d).is_some())
            {
                continue;
            }
            let kind = step["kind"].as_str().unwrap_or("tool");
            if step.get("when").is_some_and(|v| !v.is_null()) {
                let condition = &step["when"];
                let value = resolve(&json!({"$ref":condition["source"]}), &values)?;
                if value != condition["equals"] {
                    self.nodes.get_mut(name).unwrap().status = "skipped".into();
                    continue;
                }
            }
            let input = resolve(step.get("input").unwrap_or(&json!({})), &values)?;
            let target = step["target"].as_str().unwrap_or("");
            let invocation = format!("{}:{}", self.run_id, name);
            if kind == "transform" {
                let n = self.nodes.get_mut(name).unwrap();
                n.status = "succeeded".into();
                n.output = input;
                self.sequence += 1;
                continue;
            }
            let capability = if kind == "tool" {
                target.to_owned()
            } else {
                format!("step.{kind}")
            };
            let effect = self
                .grants
                .get(&capability)
                .ok_or(format!("host capability not granted: {capability}"))?;
            let approval =
                effect == "write" || step["requires_approval"] == true || kind == "approval";
            let n = self.nodes.get_mut(name).unwrap();
            n.status = if approval {
                "waiting_approval"
            } else {
                "running"
            }
            .into();
            n.attempt += 1;
            self.sequence += 1;
            actions.push(json!({"schema_version":1,"invocation_id":invocation,"step_id":name,"kind":kind,"target":target,"input":input,"requires_approval":approval,"spec":step}));
        }
        for action in &actions {
            self.outbox
                .insert(action["step_id"].as_str().unwrap().into(), action.clone());
        }
        Ok(actions)
    }
    pub fn apply(&mut self, event: &Value) -> Result<(), String> {
        let name = event["step_id"].as_str().ok_or(error("step_id required"))?;
        if event["invocation_id"] != format!("{}:{}", self.run_id, name) {
            return Err(error("invocation mismatch"));
        }
        let node = self.nodes.get_mut(name).ok_or(error("unknown step"))?;
        match event["type"].as_str() {
            Some("approve") if node.status == "waiting_approval" => node.status = "running".into(),
            Some("deny") if node.status == "waiting_approval" => node.status = "cancelled".into(),
            Some("complete") if node.status == "running" => {
                node.status = "succeeded".into();
                node.output = event["output"].clone();
            }
            Some("complete") if node.status == "succeeded" && node.output == event["output"] => {
                return Ok(())
            }
            Some("fail") if node.status == "running" => {
                node.status = "failed".into();
                node.error = Some(event["error"].as_str().unwrap_or("host error").into());
            }
            Some("recover") if node.status == "running" => {
                // Never automatically repeat effects after a crash. Host must reconcile the stable invocation ID.
                node.status = "needs_attention".into();
            }
            Some("reconcile")
                if node.status == "needs_attention"
                    && event["receipt"].as_str().is_some_and(|s| !s.is_empty()) =>
            {
                node.status = "succeeded".into();
                node.output = event["output"].clone();
            }
            _ => return Err(error("invalid state transition")),
        }
        if matches!(node.status.as_str(), "succeeded" | "failed" | "cancelled") {
            self.outbox.remove(name);
        } else if node.status == "running" {
            if let Some(a) = self.outbox.get_mut(name) {
                a["requires_approval"] = json!(false);
            }
        }
        self.sequence += 1;
        Ok(())
    }
}
pub fn dispatch(request: Value) -> Result<Value, String> {
    let op = request["op"].as_str().ok_or(error("op required"))?;
    let mut state = if op == "create" {
        Snapshot::new(
            request["run_id"]
                .as_str()
                .ok_or(error("run_id required"))?
                .into(),
            request["workflow"].clone(),
            serde_json::from_value(request["grants"].clone()).map_err(|e| e.to_string())?,
        )?
    } else {
        serde_json::from_value::<Snapshot>(request["state"].clone()).map_err(|e| e.to_string())?
    };
    let actions = match op {
        "next" => state.next_actions()?,
        "apply" => {
            state.validate()?;
            state.apply(&request["event"])?;
            vec![]
        }
        "create" => vec![],
        _ => return Err(error("unknown operation")),
    };
    Ok(json!({"state":state,"actions":actions}))
}
fn dispatch_text(text: &str) -> String {
    let result = serde_json::from_str(text)
        .map_err(|e| e.to_string())
        .and_then(dispatch);
    serde_json::to_string(&match result {
        Ok(v) => v,
        Err(e) => json!({"error":e}),
    })
    .unwrap()
}
/// Caller owns returned UTF-8 allocation and must release it with eah_free.
///
/// # Safety
/// `input` must be null or point to a valid NUL-terminated UTF-8 string for this call.
#[no_mangle]
pub unsafe extern "C" fn eah_dispatch(input: *const c_char) -> *mut c_char {
    let result = std::panic::catch_unwind(|| {
        if input.is_null() {
            return String::from("{\"error\":\"null input\"}");
        }
        match CStr::from_ptr(input).to_str() {
            Ok(s) => dispatch_text(s),
            Err(_) => String::from("{\"error\":\"invalid UTF-8\"}"),
        }
    })
    .unwrap_or(String::from("{\"error\":\"core failure\"}"));
    CString::new(result).unwrap().into_raw()
}
#[no_mangle]
/// Release a buffer returned by `eah_dispatch`.
///
/// # Safety
/// `value` must be null or an unfreed pointer returned by `eah_dispatch`.
pub unsafe extern "C" fn eah_free(value: *mut c_char) {
    if !value.is_null() {
        drop(CString::from_raw(value));
    }
}
#[cfg(feature = "android")]
#[no_mangle]
pub extern "system" fn Java_ai_easyagent_mobile_Core_dispatch(
    mut env: jni::JNIEnv,
    _class: jni::objects::JClass,
    input: jni::objects::JString,
) -> jni::sys::jstring {
    let result = match env.get_string(&input) {
        Ok(value) => dispatch_text(&String::from(value)),
        Err(_) => String::from("{\"error\":\"invalid JNI input\"}"),
    };
    env.new_string(result)
        .map(|s| s.into_raw())
        .unwrap_or(std::ptr::null_mut())
}
