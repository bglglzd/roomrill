# Lease rules

Roomrill arbitrates synthetic room light and media state. Requests are immediate-start leases with an owner label, role, priority, duration and restore policy. Member priority is0–50, operator0–100; duration is1–86400seconds. This local tool does not authenticate those role labels as remote users.

Light is one atomic vector: on with brightness1–100, or off with brightness0. Media is playing, paused or stopped. A request can target either scope or both. Each scope chooses its highest-priority eligible lease, breaking ties by the later durable submission sequence. A lease must remain uncancelled, unsuppressed and unexpired. Exact repeated submission IDs are idempotent; changed content under the same ID is refused.

Manual changes increment the whole scope's device version, even if the value stays equal, and suppress every existing lease on that scope. Unknown external changes do the same without attributing them to a human. Another scope remains independent. A newly approved request may use that newer state as its baseline.

The controller keeps last-observed state separately while a room is disconnected. Reconnect observes external changes and resolves current leases before authorizing commands. Pending desired commands bind the deciding lease, resolver generation, device version and a five-second deadline. At application, the simulated device atomically checks all of them. Expired, cancelled, superseded, stale-version, disconnected and duplicate deliveries cannot execute a new desired target.

An acknowledged automatic change captures a baseline and ownership fence. Automatic winner changes preserve that baseline. With no eligible lease, compare policy may restore only while exact acknowledged state/version and uninterrupted ownership remain proven; leave policy closes without restoring. Closure is a separate guarded action justified by the ended lease's restore policy, not a replay of its expired target. A new winner or external version invalidates queued closure.

An already-satisfied unowned request does not acquire ownership. An owned no-op can adopt a successor's policy without a device write or version increment. Repeated no-ops do not fabricate device writes. Every applied or refused command has an inspectable reason; an actual receipt binds the command, deciding lease or closure proof, time and before/after versions.

SQLite retains leases, observations, ownership, commands and receipts across process restart. The simulator's device CAS and ledger share a write transaction; this is explicitly a simulator-local guarantee. Operational time is UTC and refuses clock rollback on mutation. Demo time advances only through labelled synthetic controls. Fixture-clock intervals are not wall-time latency measurements.

Model output is only a draft. Closed-schema validation rejects unknown rooms, actions/keys, invalid light vectors, duplicate keys, nonfinite numbers and role/owner supplied by a model. Creating a lease requires a separate operator action. No arbitrary service calls, locks, access controls or safety-critical devices are supported.
