# ADR-0007: Repo Layout

## Status
Accepted

## Decision
Single monorepo:

```
UAS-SYSTEM/
├── docs/adr/                    architecture decision records
├── simulation/sitl/             Phase-1 MAVLink simulator (see ADR-0001)
├── services/telemetry-gateway/  MAVLink <-> internal event translation
├── services/api/                REST + WebSocket backend, persistence
├── apps/gcs-web/                minimal GCS frontend
├── infra/                       docker-compose for local/full-stack runs
├── scripts/                     local run + smoke-test helpers
└── .github/workflows/           CI
```

## Rationale
Small team, tight coupling between the gateway and API in early phases —
changing the telemetry schema touches both, and reviewing that as one PR in
one repo beats coordinating across repos. Each service still has its own
`requirements.txt`/`Dockerfile` so it can be split into its own repo later
without restructuring its internals, if fleet size or team size ever
justifies it.
