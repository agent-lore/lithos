"""Process-lifecycle validation for server startup and shutdown."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path


class TestServerLifecycleValidation:
    """Validation checks that catch process-exit regressions from leaked handles."""

    def test_initialize_shutdown_exits_cleanly_in_subprocess(self, temp_dir: Path) -> None:
        """A real server init/shutdown cycle should let the interpreter exit promptly.

        Regression guard for #172: persistent aiosqlite connections can leave
        worker threads alive after test completion if shutdown misses a handle.
        Running the lifecycle in a subprocess lets this test fail on the exact
        symptom CI saw: the process never exits even though the test body ended.
        """
        repo_root = Path(__file__).resolve().parents[1]
        script = textwrap.dedent(
            f"""
            import asyncio
            from pathlib import Path

            from lithos.config import LithosConfig, StorageConfig
            from lithos.server import LithosServer


            class DummySearch:
                def ensure_semantic_backend_healthy(self):
                    return True, None

                def needs_initial_rebuild(self):
                    return False


            async def main() -> None:
                root = Path({str(temp_dir)!r})
                for index in range(3):
                    config = LithosConfig(
                        storage=StorageConfig(data_dir=root / f"run-{{index}}")
                    )
                    config.ensure_directories()
                    server = LithosServer(config)
                    server.search = DummySearch()
                    server.graph.load_cache = lambda: True
                    server._enrich_worker = None

                    await server.initialize()

                    assert server.memory._stats_store._db is not None
                    assert server.edge_store._db is not None

                    await server.memory._stats_store.increment_node_stats(node_id=f"node-{{index}}")
                    await server.edge_store.upsert(
                        from_id=f"from-{{index}}",
                        to_id=f"to-{{index}}",
                        edge_type="related",
                        weight=0.5,
                        namespace="default",
                    )

                    await server.shutdown()

                    assert server.memory._stats_store._db is None
                    assert server.edge_store._db is None

                print("lifecycle-ok")


            asyncio.run(main())
            """
        )
        env = os.environ.copy()
        src_path = str(repo_root / "src")
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            src_path
            if not existing_pythonpath
            else os.pathsep.join((src_path, existing_pythonpath))
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "lifecycle-ok" in result.stdout
