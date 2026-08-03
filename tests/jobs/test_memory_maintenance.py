"""P23: Memory maintenance job — acceptance tests."""

import asyncio
import time
from unittest.mock import MagicMock

from jobs.memory_maintenance import MemoryMaintenanceJob


class TestMemoryMaintenance:

    def test_thread_start_stop(self):
        store = MagicMock()
        store.prune_expired.return_value = 3
        job = MemoryMaintenanceJob(store, interval_s=3600)
        job.start_thread()
        time.sleep(0.2)
        job.stop_thread()
        # At least one cycle should have run (or thread started cleanly)
        assert job._thread is not None

    def test_async_start_stop(self):
        store = MagicMock()
        store.prune_expired.return_value = 0
        job = MemoryMaintenanceJob(store, interval_s=3600)

        async def _run():
            task = await job.start_async()
            await asyncio.sleep(0.2)
            await job.stop_async()

        asyncio.run(_run())

    def test_none_store_survives(self):
        job = MemoryMaintenanceJob(None)
        job._run_one_cycle()  # should not raise
