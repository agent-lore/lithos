# Fix #115: link re-index on document update

*2026-03-30T02:20:30Z by Showboat 0.6.1*
<!-- showboat-id: 3cc91323-3197-407c-80f6-63c4db75ffcb -->

PR fix/issue-115-link-reindex-on-update ensures that when a document is updated, its outgoing graph edges are rebuilt without dropping incoming edges from other documents that link to it.

```bash
uv run --extra dev pytest tests/test_file_watcher_events.py::TestFileWatcherEventEmission::test_file_change_update_rebuilds_graph_edges -v 2>&1 | sed 's/[0-9]*\.[0-9]*s *(0:[0-9:]*)//g' | sed 's/ in [0-9]*\.[0-9]*s/ in Xs/g' | sed 's|-- /.*\.venv/bin/python3|-- .venv/bin/python3|g' | sed 's|rootdir: /.*|rootdir: .|g'
```

```output
============================= test session starts ==============================
platform darwin -- Python 3.12.12, pytest-9.0.2, pluggy-1.6.0 -- .venv/bin/python3
cachedir: .pytest_cache
rootdir: .
configfile: pyproject.toml
plugins: anyio-4.12.1, asyncio-1.3.0, cov-7.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 1 item

tests/test_file_watcher_events.py::TestFileWatcherEventEmission::test_file_change_update_rebuilds_graph_edges PASSED [100%]

============================== 1 passed in Xs ===============================
```
