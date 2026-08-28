# ADR-0006: The judge's door is CloudFront over a public Lambda Function URL, and it polls

Date: 2026-08-25. Status: accepted.

## Context

ADR-0005 named this as the single largest work item the AgentCore decision creates, and
research against AWS documentation confirmed it is not avoidable.

**A browser with no credentials cannot reach AgentCore Runtime.** `InvokeAgentRuntime`
accepts IAM SigV4 or an OAuth 2.0 bearer token, and the JWT authorizer requires at least
one of allowed audiences, clients, scopes, or custom claims. There is no anonymous mode
and no pre-shared mode. AgentCore Gateway does not help either, because its inbound auth
is the same. So a server-side component must hold the credential and invoke the runtime
on the visitor's behalf. That component is the door.

The door also has to carry this product's actual shape. A run pauses for an attorney and
resumes later, which spike 0001 proved works across separate invocations on fresh
compute. So the door needs to start a run, show a pending-approval state, take an
approve or defer answer, and resume. Runs take tens of seconds.

Four documented limits, each read at source, remove most of the design space:

1. **Python cannot response-stream on a managed Lambda runtime.** The documentation is
   explicit: "Lambda supports response streaming on Node.js managed runtimes. For other
   languages, including Python, you can use a custom runtime with a custom Runtime API
   integration to stream responses or use the Lambda Web Adapter."
2. **API Gateway HTTP API caps integration timeout at 30 seconds and it cannot be
   raised.** That is below our run length.
3. **Origin Access Control forces `AuthType AWS_IAM`**, and a signed POST then requires
   `x-amz-content-sha256`, which a plain browser `fetch` will not send.
4. **Streamed responses bill for the full function duration and are not stopped when the
   client disconnects.** A judge closing the tab does not stop the meter.

## Decision

**One CloudFront distribution with two origins**: Amazon S3 serving the static operator
console, and a **Lambda Function URL with `AuthType NONE`** behind an `/api/*` behavior.
The door is **asynchronous: start, then poll**, rather than holding a connection open.

Consequences of that shape, in order of why it was chosen:

- **Same origin removes CORS entirely.** The console and the API share a hostname, so
  there is no preflight, no duplicate-header conflict between the Function URL's CORS
  configuration and headers set in code, and no third-party cookie question.
- **Polling sidesteps every timeout ceiling.** No 30 second integration cap, no 30
  second origin response timeout to tune, and no dependency on streaming from Python.
  A start call returns an identifier immediately; the page polls a status endpoint.
- **Polling also stops paying for abandoned runs.** Since a streamed response bills for
  the full duration regardless of the client, a design that never holds the connection
  open cannot be billed for a judge who closed the tab.
- **CloudFront is the only place AWS WAF can attach in this stack.** WAF cannot attach
  to an HTTP API, and CloudFront Functions cannot rate limit because network access and
  timers are restricted there.
- The Function URL stays `NONE` rather than `AWS_IAM` with OAC, precisely to avoid
  limit (3). It is protected from direct-origin bypass by a **secret custom origin
  header** that CloudFront adds and the function requires, which is the documented
  pattern.

**Abuse protection is a function-side spend counter, not WAF.** A WAF web ACL is a fixed
**$6.00 per month** ($5 for the ACL, $1 for one rule) against roughly $0 of usage-based
cost for everything else at demo volume.

### Amended 2026-08-25, after the first deploy failed

This decision originally named **reserved concurrency** as the control. That is not
available here, and the correction is recorded rather than quietly edited away because
the reasoning changed.

The first deploy failed outright:

```
Specified ReservedConcurrentExecutions for function decreases account's
UnreservedConcurrentExecution below its minimum value of [10]
```

This account's **total** Lambda concurrency limit is **10**, not the familiar 1000, so
reserving any of it drops unreserved concurrency below the required minimum and the
`AWS::Lambda::Function` resource is rejected. The account ceiling is therefore already
the concurrency cap, imposed from above and not by us.

That turned out to sharpen the design rather than weaken it. **Concurrency was never the
right thing to bound; spend was.** `/api/stats` is pure arithmetic over 48 records and
costs nothing however often it is called, so capping it would only make the checkable
number less checkable. `/api/run` invokes a model and is the only endpoint that can
spend money. The cap now lives there, as an atomic DynamoDB counter keyed on the day,
and a caller who reaches it gets a `429` that states the count, the cap, and the fact
that `/api/stats` is never capped.

One process note worth keeping: the failing deploy reported **exit code 0**, because the
command was piped to `tail` and the pipeline returned tail's status. The failure was
found by reading the log, not by trusting the exit code. Gate commands run with no pipe
on the exit path.

## Consequences

- **The door is where the checkable number lives.** A claim nobody can check is not
  evidence. The door must expose the headline figure on an unauthenticated endpoint that
  recomputes it per request, stated in the clinic's own currency (deadlines a hand count
  gets wrong) rather than in milliseconds, so a stranger with no key can count the rows
  and disagree.
- **`runtimeSessionId` has a 33 character minimum**, confirmed empirically in spike 0001
  by trying 32 and 33. The door generates these, so it must not use a short identifier.
- **A cold session costs a visitor several seconds on their first click.** Spike 0001
  measured cold start at 4.1 to 5.1 seconds wall clock against 0.15 seconds warm, nearly
  all of it microVM provisioning. AgentCore isolates a session per caller, so there is no
  pool to pre-warm; the console shows an honest indeterminate loader instead of pretending
  the wait is not happening.
- **Every bucket the deployed agent touches needs an explicit execution role grant.**
  The generated role has none, and spike 0001 only surfaced that because the runtime was
  made to refuse a run it could not persist.
- Two figures could not be established from documentation and must be measured if they
  become load bearing: the Lambda Function URL request timeout as distinct from the
  function timeout, and whether CloudFront buffers server-sent events from a custom
  origin. The polling design makes neither of them matter today, which is a further
  argument for it.
