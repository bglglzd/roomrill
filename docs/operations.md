# Operations

## Local workspaces

Use Python 3.11 or newer and a private writable directory. The package has no runtime dependencies. The [README](../README.md) describes source installation and the UI.

```sh
roomrill --db work.sqlite init
roomrill --db work.sqlite create examples/meeting-lease.json
roomrill --db work.sqlite state
roomrill --db work.sqlite manual examples/manual-light.json
roomrill --db work.sqlite serve
```

These are real simulator operations using current UTC time. `init` refuses an existing file. Operational `serve` reconciles roughly once per second; OS scheduling and SQLite work add overhead. It is not a hard real-time controller. Stop the owned server with Ctrl+C. No service is installed.

`roomrill demo --serve` instead uses a labelled synthetic clock, seeded request and `.roomrill/demo.sqlite`. Rerunning resumes the ledger. Choose a new path with `roomrill --db fresh-demo.sqlite demo --serve`; nothing is deleted. The initial request lasts 1,200 fixture seconds. Apply a manual light value, then advance time to observe its preservation and media closure.

## Commands and recovery

```sh
roomrill --db work.sqlite delivery --manual
roomrill --db work.sqlite reconcile
roomrill --db work.sqlite state
roomrill --db work.sqlite deliver COMMAND_ID
roomrill --db work.sqlite cancel call-001
roomrill --db work.sqlite link meeting --offline
roomrill --db work.sqlite link meeting --online
```

Copy an actual pending command ID from state; IDs are not templates to reuse. Manual delivery attempts precisely that ID. After a deadline refusal, reconcile explicitly to create a new command if its lease is still eligible. Automatic delivery can be restored with `delivery --automatic`.

| State | Next action |
| --- | --- |
| Device disconnected | Inspect actual versus last-observed state. Reconnect, observe external changes and reconcile current intent. |
| Suppressed request | Preserve the newer manual/external choice. Create a new request only after reviewing that choice. |
| Refused or superseded command | Inspect its reason. Reconcile; never replace an old ID silently. |
| Changed local session | The UI refreshes token/state. Review the fresh view, then retry explicitly. |
| Clock rollback | Correct the operational clock. Reads retain the last accepted selection; new mutations are refused. |
| Storage/format/integrity failure | Preserve the file. Check permissions and other writers before assuming corruption. No automatic repair or migration is attempted. |

Back up the database while no writer is active, or use SQLite's backup facilities. There is no retention/deletion command. UI history truncation is a view limit, not deletion. Room-level disconnection affects both light and media; ownership and manual suppression remain per scope.

## Optional model draft

A model is unnecessary for manual leases. Configure an existing local chat server when a text draft is useful:

```sh
roomrill demo --serve --model-endpoint http://127.0.0.1:8080/v1/chat/completions --model YOUR_MODEL_ID
```

The supported tested profile is llama.cpp's OpenAI-shaped chat endpoint with `response_format.json_schema` and `chat_template_kwargs`. This is not a claim that every chat provider implements those fields. The client requests temperature 0, seed 42 and non-thinking output, preserves schema property order, and validates the returned draft itself. Different models can decline or misinterpret a request; inspect every field.

For an explicit example: “In room meeting, turn the light on at brightness 60 and set media to paused for 20 minutes. Priority 30, restore policy compare.” The draft fills editable controls. **Create lease is a separate operator action.** Invalid model JSON creates no lease; a model can request clarification, and its interpretation still requires review. A model outage does not alter the ledger.

Only operator-configured HTTPS or numeric-loopback HTTP endpoints are allowed. Credentials in URL/query/fragment are refused. There are no redirects, ambient proxies or implicit retries. The parent allows at most 45 seconds and reaps only its own HTTP worker; response size is 64 KiB. HTTPS uses normal certificate checks. Use `--model-key-env ENVIRONMENT_VARIABLE_NAME` only when that configured server requires an API key; its value is not put in arguments, state or logs.

Live verification used [Qwen3-4B Q4_K_M](https://huggingface.co/Qwen/Qwen3-4B-GGUF) and llama.cpp build 12211. Model weights/runtime are not included in releases. No Home Assistant instance or physical device is controlled. [Comparison and integration boundary](comparison.md).
