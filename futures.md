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
