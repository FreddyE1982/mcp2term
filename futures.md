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
- **Usage:** Expose a plugin registration API so profiles can be installed at runtime (for example from MCP plugins) and forwarded to the client during handshake. Profiles should support validation hooks, remote capability negotiation, and documentation discovery so operators always understand which transformations are active before issuing edits.

## File operation conflict detection
- **Purpose:** Detect and prevent conflicting edits when multiple clients edit the same file concurrently through the MCP tools.
- **Usage:** Introduce optimistic concurrency controls to `FileEditor` that compute content hashes prior to mutation and verify they still match when applying edits. Expose the checksums through `FileOperationResult` so plugins and clients can warn operators about potential conflicts and offer auto-merge strategies.

## Client onboarding banner plugins
- **Purpose:** Allow deployments to tailor the introductory message shown after connecting, injecting organisation-specific guidance, compliance prompts, or links to documentation without editing the core client.
- **Usage:** Extend the intro banner provider registry with plugin-discovered providers that can append new sections or rewrite existing ones. Plugins could surface mandatory security reminders, company hotkeys, or dynamic status indicators fetched from monitoring APIs while preserving the default capability overview for new operators.

## Persistent user chat transcripts
- **Purpose:** Preserve the auxiliary terminal chat history across server restarts so operational directives sent from the supervising user remain auditable.
- **Usage:** Extend the `UserChatBridge` with pluggable transcript writers that stream each emitted message into structured storage (for example, newline-delimited JSON). Provide rotation policies, remote sinks (such as syslog or HTTP POST targets), and tooling to replay transcripts into the MCP logging bus when a client reconnects mid-session.

## Interactive chat status mirroring
- **Purpose:** Surface delivery acknowledgements, failure diagnostics, and plugin-generated notices inside the terminal helper process so operators receive immediate visual feedback without consulting logs.
- **Usage:** Expand the inter-process control channel to forward server-generated status messages using the existing append envelope. Add bridge hooks that format status updates, append them to the console history, and optionally highlight warnings or errors with colour-coded text for rapid operator assessment.

## Console messaging hotkey profiles
- **Purpose:** Allow deployments to customise the activation key, cancellation bindings, and prompt phrasing used by the console messaging bridge so that they align with internal runbooks or avoid clashing with terminal shortcuts.
- **Usage:** Extend `ServerConfig` with a console messaging profile that defines the activation key, cancellation keys, and prompt text. The profile should be validated for cross-platform compatibility and exposed through the plugin registry so administrators can ship environment-specific presets.

## Operator message policy hooks
- **Purpose:** Enable security and compliance teams to approve, redact, or transform operator-sent messages before they reach clients, ensuring that sensitive guidance does not leak unintentionally.
- **Usage:** Introduce a plugin hook fired immediately before `_broadcast_message` transmits to sessions. The hook should receive the raw message, the formatted prefix, and contextual metadata such as the timestamp and connected session count. Plugins can veto delivery, substitute content, append audit annotations, or trigger out-of-band notifications when policy rules are violated.

## Operator messaging command-line companion
- **Purpose:** Provide a non-interactive utility that can inject operator messages into the running server now that the standalone terminal window has been retired.
- **Usage:** Implement a small CLI entry point (for example `mcp2term-send-message`) that connects to the existing chat bridge transport, authenticates using short-lived tokens, and submits structured messages. The tool should support templated payloads, dry-run validation, and integration with automation systems so supervisors can broadcast scripted status updates without requiring physical access to the server console.

## Console stream observability hooks
- **Purpose:** Expose metrics and structured events from the new pause-aware console stream proxies so that monitoring systems can alert when output buffering persists or grows unexpectedly.
- **Usage:** Introduce a registry of observers that receive callbacks whenever `ConsoleStreamProxy` transitions between paused and active states or flushes buffered output. Observers should record buffering duration, data volume, and caller metadata so operators can diagnose slow consumers or misbehaving plugins. Provide sample integrations for Prometheus and OpenTelemetry exporters.

## Warning delivery health monitoring
- **Purpose:** Detect when client-side warning sinks repeatedly fail so operators can proactively repair integrations before users lose critical diagnostics.
- **Usage:** Track notice-writer failures within `RemoteMcpSession`, exposing counters and backoff strategies through telemetry hooks. When repeated failures occur, escalate by surfacing a consolidated status banner to the interactive shell and emitting structured events to plugins so downstream observability pipelines can alert maintainers.
