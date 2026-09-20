// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

use serde_json::{json, Value};
use std::io::{self, BufRead};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let line = io::stdin()
        .lock()
        .lines()
        .next()
        .ok_or("missing request")??;
    let request: Value = serde_json::from_str(&line)?;
    let args = &request["params"]["arguments"];
    let value = args["a"].as_f64().ok_or("a must be numeric")?
        + args["b"].as_f64().ok_or("b must be numeric")?;
    println!(
        "{}",
        json!({"id":request["id"],"result":{"value":value,"language":"Rust"}})
    );
    Ok(())
}
