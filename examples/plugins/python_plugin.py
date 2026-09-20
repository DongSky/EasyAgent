# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0

import json
import sys

request = json.loads(sys.stdin.readline())
args = request["params"]["arguments"]
print(json.dumps({"id": request["id"], "result": {"value": args["a"] + args["b"], "language": "Python"}}))
