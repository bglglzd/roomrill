# Related tools

Checked 2026-11-11 against primary documentation. This is a specific implemented lease policy and simulator, not a uniqueness claim or a replacement automation platform.

| Tool | Existing capability | Roomrill's implemented scope |
| --- | --- | --- |
| [Home Assistant scenes](https://www.home-assistant.io/integrations/scene/) | Grouped target states and dynamic snapshots for restoration. | Typed priority arbitration, explicit scope ownership and guarded pending/closure commands in a durable simulator. |
| [Home Assistant timers](https://www.home-assistant.io/integrations/timer/) | Countdown actions/events and optional restoration across startup. An already elapsed stopped timer does not emit a startup `timer.finished` event. | Recompute currently eligible leases on restart/reconnect; do not depend on replaying a timer event. |
| [AppDaemon](https://appdaemon.readthedocs.io/en/latest/) | A Python event-driven automation environment where custom policies can be built. | A small fixed policy with observable arbitration, version/fence checks and reproducible synthetic cases. |

Home Assistant's ordinary service call has no expected-version/CAS parameter. A read followed by a restore call therefore cannot establish the same atomic guarantee as this simulator. `POST /api/states` updates HA's state representation; it is not proof of physical device actuation. Context/user identity may be absent or ambiguous, so unknown changes must not be confidently called human overrides.

HA integration remains future work and is not advertised as supported in this release. Primary references: [WebSocket service calls](https://developers.home-assistant.io/docs/api/websocket/#calling-a-service), [REST API](https://developers.home-assistant.io/docs/api/rest/), [state/context model](https://contributors.home-assistant.io/data/reference/data-model/).
