// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

use std::io::{self, BufRead};
fn main() {
    for line in io::stdin().lock().lines() {
        let result = line
            .map_err(|e| e.to_string())
            .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()))
            .and_then(easyagent_core::dispatch);
        println!(
            "{}",
            match result {
                Ok(v) => v,
                Err(e) => serde_json::json!({"error":e}),
            }
        );
    }
}
