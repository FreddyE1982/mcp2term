# Qt GUI Threading Requirements

## Source
- Qt Documentation: [Threads and QObjects](https://doc.qt.io/qt-5/threads-qobject.html)

## Key Points
- Qt GUI classes such as `QWidget` and its subclasses are not reentrant and must be used from the main thread.
- `QCoreApplication::exec()` (and by extension `QApplication::exec()`) must be invoked from the main thread.
- Long-running work should be delegated to worker threads, with results posted back to the GUI thread once complete.
- Event loops are thread-specific; creating timers or relying on `deleteLater()` requires an active event loop in the owning thread.

## Implications for mcp2term
- The PyQt-based chat bridge must ensure that the `QApplication` object is both constructed and executed on the main thread.
- Worker threads should communicate with the GUI thread via queued signals or thread-safe primitives instead of directly managing the event loop.
