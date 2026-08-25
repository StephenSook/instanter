# AgentCore interrupt spike

The smallest program that answers the question ADR-0005 left open: does a Strands
`multiagent.Graph` interrupt survive Amazon Bedrock AgentCore Runtime, and can it be
resumed from a separate invocation on fresh compute?

Findings and measurements: [`docs/spikes/0001-agentcore-interrupt.md`](../../docs/spikes/0001-agentcore-interrupt.md).

This directory is a spike, not a product path. It keeps its own dependency list so it
stays outside the product's dependency closure, and nothing in `engine/` or `agent/`
imports it.

## Files

| File | What it is |
|---|---|
| `main.py` | The runtime. Two node graph, a gated tool, an approval hook, and four probe actions (`ping`, `start`, `resume`, `raw`) |
| `probe_deployed.py` | Drives the deployed runtime through `InvokeAgentRuntime` and prints the verdict |
| `requirements.pyproject.toml` | The spike's dependencies, kept separate from the project's `pyproject.toml` |

## Running it

Locally, with a bucket the caller can write to:

```sh
export AWS_REGION=us-east-1
export SPIKE_BUCKET=<your-bucket>
python main.py            # serves the AgentCore HTTP contract on :8080
curl -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"action":"start","session_id":"run-1","resume_mode":"explicit"}'
```

The response carries the interrupt id. Answer it on a second call:

```sh
curl -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"action":"resume","session_id":"run-1","resume_mode":"explicit",
       "interrupt_id":"<id from above>","response":"approve"}'
```

`resume_mode` selects the persistence path: `explicit` replays a state document the
spike writes itself, `session` relies on `S3SessionManager`, and `none` is the negative
control that must fail.

## Deploying

Deploying needs the AgentCore CLI (`npm install -g @aws/agentcore`) and an
`agentcore add agent --type byo --build CodeZip --framework Strands --model-provider
Bedrock`. Two things the CLI will not do for you, both of which failed silently the
first time:

**Environment variables do not travel through `agentcore.json`.** An `environment` map
added there passed `agentcore deploy --dry-run` validation and the container still read
an empty value. Set them on the control plane instead:

```sh
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id <id> \
  --environment-variables SPIKE_BUCKET=<bucket> \
  --agent-runtime-artifact <current artifact> \
  --role-arn <current role> \
  --network-configuration <current network config>
```

**The generated execution role has no access to your bucket.** Attach a policy scoped
to it, or every persist fails with `AccessDenied`:

```sh
aws iam put-role-policy --role-name <execution-role> \
  --policy-name SpikeStateBucket --policy-document file://policy.json
```

The runtime refuses to start a run it cannot persist, which is what made both of these
visible rather than leaving a green run with no durable state behind it.

Then point the probe at the deployment:

```sh
export SPIKE_RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes \
  --query 'agentRuntimes[0].agentRuntimeArn' --output text)
python probe_deployed.py
```
