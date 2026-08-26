"""CDK entry point for the judge's door.

cd infra
# On a live stack, reuse the existing Lambda ORIGIN_SECRET. Do not mint a new
# one. Minting rotates CloudFront's origin header out from under the function
# and 403s every /api call until they match again.
export DOOR_ORIGIN_SECRET=...existing from the deployed DoorFunction...
export AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:REGION:ACCOUNT:runtime/instanteragent_triage-...
../.venv/bin/python build_door.py
cdk deploy
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from stack import JudgeDoorStack

app = cdk.App()
JudgeDoorStack(
    app,
    "InstanterJudgeDoor",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)
app.synth()
