# Instanter operator console

The web console a clinic operator watches: the live filing cabinet
(`GET /api/queue`, recomputed per request), the checkable headline
(`GET /api/stats`), Sweep the queue (a real agent run on Bedrock AgentCore
that stops at the attorney interrupt), the what-if paper calendar, summons
OCR intake, and Web Push opt-in for interrupt pings.

React 19 + Vite + Tailwind. Every number rendered here is engine output
fetched from the door; the UI never invents a date, a day count, or a rank.
`public/queue.json` is a labelled snapshot used only when the door is
unreachable, and the cabinet says so on screen when that happens.

```
npm ci
npm run lint
npm test
npm run build
```

CI runs exactly those commands (`.github/workflows/ci.yml`, job `web`). The
built site deploys to S3 behind CloudFront via the CDK stack in `../infra`.
