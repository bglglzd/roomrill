# Local UI interface

All state below is synthetic. GET `/api/state` returns `mode`, `now`, `automatic_delivery`, `rooms`, `leases`, `commands`, `events`. A room has `id`, `name`, `online`, `scopes` with `light` and `media`. Each scope has `device:{value,version}`, `observed:{value,version}`, `winner` (lease or null), `ownership` (or null), `pending` (command list). Commands contain `id`, `room`, `scope`, `kind` (`desired`/`restore`), `lease_id`, `target`, `expected_version`, `deadline`, `status`, `reason`. Leases contain submitted fields plus `sequence`, `created_at`, `expires_at`, `status`, and `suppressed_scopes`.

`commands` and `events` are returned newest first, ordered by their persisted event sequence. Commands include at most the latest 100 retained records.

GET `/api/session` returns an in-memory `token`. POST uses JSON, `X-Roomrill-Token`, exact loopback Host and same Origin. Mutations return updated state unless stated otherwise. Errors are JSON `{error:{code,message}}` with non-2xx status.

- `/api/leases`: `{id,room,owner,role,priority,duration_seconds,restore_policy,targets}`. Light target is `{on,brightness}`; media string.
- `/api/cancel`: `{id}`.
- `/api/manual`: `{room,scope,value}`; brightness slider must submit a whole canonical light vector.
- `/api/link`: `{room,online}`.
- `/api/reconcile`: `{}`.
- `/api/delivery`: `{automatic:boolean}`; explicit synthetic delayed-ack control.
- `/api/deliver`: `{id}` for an individual pending command.
- `/api/advance`: `{seconds}`; demo fixture only, integer from 0 to 86,400.
- `/api/draft`: `{text}` returns `{draft:{room,targets,priority,duration_seconds,restore_policy},model,requires_confirmation:true}`; it never mutates the engine. Endpoint/model configuration comes from server CLI, never request JSON. Without endpoint returns a clear model_not_configured error.

The UI must show labelled actual simulator and last observed state, current winner/reason, valid/ended/suppressed leases, pending commands and receipts/events. Controls change relevant state; no slogans or hero section. Demo mode label is `Synthetic local simulator`. Read-only recorded mode uses `Recorded synthetic example`, a saved-stage selector and disabled mutations with local-run instructions. Recorded assets may read `recorded.json` as `{format:1,stages:[{id,label,state}]}`. No token or live endpoint is recorded. Raw JSON can be collapsed beneath legible essential state.
