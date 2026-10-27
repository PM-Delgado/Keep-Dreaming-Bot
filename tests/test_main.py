import asyncio
import os
import signal
import sys

import pytest

from bot.__main__ import stop_on_sigterm


@pytest.mark.skipif(sys.platform == "win32", reason="loop signal handlers do not exist on Windows")
async def test_sigterm_cancels_the_bot_task_so_it_can_close_cleanly():
    task = asyncio.create_task(asyncio.sleep(60))
    stop_on_sigterm(task)
    os.kill(os.getpid(), signal.SIGTERM)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
