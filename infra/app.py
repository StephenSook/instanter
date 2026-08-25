"""CDK entry point for the judge's door.

cd infra
export DOOR_ORIGIN_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
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
