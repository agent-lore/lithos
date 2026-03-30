# Logging Demo — coordination.py and knowledge.py

*2026-03-30T02:19:02Z by Showboat 0.6.1*
<!-- showboat-id: b223cd41-6ec0-462d-80d7-b03d07b29906 -->

Demonstrates that DEBUG-level log lines fire correctly in both `coordination.py` and `knowledge.py` after the fix/issues-91-92-logging changes.

### knowledge.py — slugify DEBUG

```bash
uv run python3 -c "
import logging, sys
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format=\"%(name)s %(levelname)s %(message)s\")
logging.getLogger(\"lithos.knowledge\").setLevel(logging.DEBUG)
from lithos.knowledge import slugify
result = slugify(\"Hello World Test\")
print(f\"slug={result}\")
" 2>&1 | grep -E "^lithos|^slug"
```

```output
lithos.knowledge DEBUG slugify: title='Hello World Test' slug='hello-world-test'
slug=hello-world-test
```

### coordination.py — ensure_agent_known DEBUG

```bash
uv run python3 -c "
import logging, sys, asyncio, tempfile
from pathlib import Path
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format=\"%(name)s %(levelname)s %(message)s\")
logging.getLogger(\"lithos.coordination\").setLevel(logging.DEBUG)
from lithos.coordination import CoordinationService
from lithos.config import LithosConfig
async def run():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = LithosConfig()
        svc = CoordinationService(config=cfg)
        svc._db_path = Path(tmpdir) / \"coord.db\"
        await svc.initialize()
        await svc.ensure_agent_known(\"demo-agent\")
asyncio.run(run())
" 2>&1 | grep "^lithos.coordination"
```

```output
lithos.coordination DEBUG ensure_agent_known: agent_id=demo-agent
```
