# Xonsh Events API Notes

## Event registration semantics
- The `xonsh.events` module exposes a global `EventManager` instance named `events`.
- Individual events, such as `on_transform_command`, are created on-demand via attribute access on this manager.
- Event instances are callable. Calling an event with a handler function both registers the handler and attaches the `__validator` attribute expected by the dispatcher.
- Handlers should accept keyword arguments (`**kwargs`); `on_transform_command` invokes handlers with the `cmd` keyword.
- The preferred unsubscription mechanism for modern events is `event.discard(handler)`. `discard` removes the handler if present without raising when missing.

## Legacy compatibility
- Earlier xonsh versions exposed `.connect()` / `.disconnect()` methods on events instead of the callable protocol.
- Client integrations that need to support both styles should detect the available API. When `.connect()` exists, use it and pair it with `.disconnect()` for cleanup. Otherwise, call the event to register the handler and use `.discard()` for removal.

## Transform command workflow
- The interactive shell uses `xonsh.shell.transform_command()` to apply command transformations prior to execution.
- `transform_command()` repeatedly fires the `on_transform_command` event until the command stabilises or the recursion limit is reached, allowing chained transformations.
