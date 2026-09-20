// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

function handle(r){if(r.method.startsWith("lifecycle."))return {result:{}};return {result:{label:r.params.details.label,fragile:r.params.details.fragile,total:r.params.counts.reduce((a,b)=>a+b,0)}};}
