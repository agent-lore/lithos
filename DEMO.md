# Fix #121: lithos_read FileNotFoundError → structured error

*2026-03-30T02:19:16Z by Showboat 0.6.1*
<!-- showboat-id: 8ed53acc-770a-493a-9b94-9bd548b02db0 -->

PR fix/issue-121-lithos-read-filenotfound ensures lithos_read never raises a raw FileNotFoundError. Non-existent documents (by id or path) always return a structured error envelope with status=error and code=doc_not_found.

```bash
uv run --extra dev pytest tests/test_server.py::TestKnowledgeToolWorkflow::test_lithos_read_nonexistent_doc_does_not_raise tests/test_server.py::TestKnowledgeToolWorkflow::test_lithos_read_missing_id_returns_structured_error tests/test_server.py::TestKnowledgeToolWorkflow::test_lithos_read_missing_path_returns_structured_error -v 2>&1 | sed 's/[0-9]*\.[0-9]*s *(0:[0-9:]*)//g' | sed 's/ in [0-9]*\.[0-9]*s/ in Xs/g' | sed 's|-- /.*\.venv/bin/python|-- .venv/bin/python|g' | sed 's|rootdir: /.*|rootdir: .|g'
```

```output
platform darwin -- Python 3.12.12, pytest-9.0.2, pluggy-1.6.0 -- .venv/bin/python
cachedir: .pytest_cache
rootdir: .
configfile: pyproject.toml
plugins: anyio-4.12.1, asyncio-1.3.0, cov-7.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 3 items

tests/test_server.py::TestKnowledgeToolWorkflow::test_lithos_read_nonexistent_doc_does_not_raise PASSED [ 33%]
tests/test_server.py::TestKnowledgeToolWorkflow::test_lithos_read_missing_id_returns_structured_error PASSED [ 66%]
tests/test_server.py::TestKnowledgeToolWorkflow::test_lithos_read_missing_path_returns_structured_error PASSED [100%]

```
