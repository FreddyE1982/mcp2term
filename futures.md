# Futures

## Multi-session isolation
- **Purpose:** Support per-client working directories and environment sandboxes.
- **Usage:** Introduce a session manager plugin hook that allocates isolated directories and environment overlays before each command.

## Structured stdout/stderr attachments
- **Purpose:** Allow downstream consumers to retrieve large outputs without flooding log streams.
- **Usage:** Implement an MCP resource provider that persists outputs to temporary files and references them via resource URIs alongside streamed previews.

## Plugin discovery via entry points
- **Purpose:** Simplify plugin distribution by allowing packages to register under a common entry point group.
- **Usage:** Extend `PluginManager` to load entry points such as `mcp2term.plugins`, merging them with `MCP2TERM_PLUGINS` configuration.


## Ngrok metrics streaming
- **Purpose:** Emit ngrok tunnel statistics and connection diagnostics to clients and plugins.
- **Usage:** Add a background task to `NgrokController` that polls the ngrok administrative API and forwards aggregated metrics through the plugin registry for observability dashboards.

## Console echo customization templates
- **Purpose:** Allow operators to customise the console mirroring format, destination streams, and optional persistence into structured logs.
- **Usage:** Extend `ConsoleEchoListener` with configurable format strings supplied via `ServerConfig` and expose plugin hooks to replace or augment the default listener while keeping mirroring guarantees.

## Command cancellation policy plugins
- **Purpose:** Allow administrators to customise which signals are sent for cancellation, define escalation strategies, and audit cancellation attempts.
- **Usage:** Introduce a plugin hook invoked before `cancel_command` dispatches a signal so plugins can substitute signals, introduce grace periods, or capture metrics for observability dashboards.

## Backpressure telemetry publishing
- **Purpose:** Surface client and server buffering metrics to plugins and operators for proactive health monitoring.
- **Usage:** Expose the backpressure monitor state via plugin callbacks and structured metrics endpoints so dashboards can highlight when queues are building up and trigger alerts.

## Diagnostic policy plugins
- **Purpose:** Allow deployments to customise how the client probes remote endpoints (for example, toggling probe methods, capturing historical availability metrics, or enforcing retry strategies) without modifying core client code.
- **Usage:** Introduce a plugin hook invoked by the client before emitting startup diagnostics so plugins can adjust probe targets, provide additional context such as cached latency information, or short-circuit connection attempts during planned maintenance windows. The hook should receive the resolved endpoint URL and return a structured policy describing which probes to execute and how to present the resulting output to users.

## Interactive input policy plugins
- **Purpose:** Allow operators to inspect, transform, or record interactive stdin data flowing from clients to the remote server.
- **Usage:** Extend the plugin registry with hooks fired before `send_stdin` writes to subprocess pipes so plugins can redact secrets, enforce input quotas, or tee traffic into compliance archives. Policies could also modify the delivery strategy (for example, chunk sizing or encoding) without changing the core executor.

## Warning analytics dashboards
- **Purpose:** Capture and aggregate warning events emitted by the server and client so operators can monitor recurring failure patterns, correlate them with infrastructure incidents, and produce proactive alerts.
- **Usage:** Implement a plugin using the new warning listener hooks to forward warning metadata into an observability pipeline (for example, Prometheus or OpenTelemetry). Provide client-side adapters that subscribe to notice writers, batching warnings for long-term storage while keeping the interactive terminal output readable. Document configuration for routing warnings to dashboards and setting thresholds for alerting.

## File patch templating macros
- **Purpose:** Layer higher-level diffing and templating workflows on top of the existing `manage_file` tool so complex multi-file refactors can be performed reproducibly.
- **Usage:** Build on the unified diff support exposed via the new `patch` operation by layering templating DSLs, validation hooks, and preview tooling. Provide plugin hooks to validate patches, inject pre-commit checks, and broadcast file mutation events to auditing backends.

## Inline escape decoding profiles
- **Purpose:** Allow operators to customise how inline `filetool` content is normalised when it contains escape sequences (for example, turning decoding off entirely or enabling additional escape rules for binary payloads).
- **Usage:** Extend the command parser with configurable profiles that can be selected via command-line flags or plugin policies. Profiles should specify which escape sequences are recognised and whether decoding is conditional on the absence of literal newlines, ensuring administrators can strike the right balance between ergonomics and exactness for their workflows.

## File operation conflict detection
- **Purpose:** Detect and prevent conflicting edits when multiple clients edit the same file concurrently through the MCP tools.
- **Usage:** Introduce optimistic concurrency controls to `FileEditor` that compute content hashes prior to mutation and verify they still match when applying edits. Expose the checksums through `FileOperationResult` so plugins and clients can warn operators about potential conflicts and offer auto-merge strategies.

## Client onboarding banner plugins
- **Purpose:** Allow deployments to tailor the introductory message shown after connecting, injecting organisation-specific guidance, compliance prompts, or links to documentation without editing the core client.
- **Usage:** Extend the intro banner provider registry with plugin-discovered providers that can append new sections or rewrite existing ones. Plugins could surface mandatory security reminders, company hotkeys, or dynamic status indicators fetched from monitoring APIs while preserving the default capability overview for new operators.
