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
