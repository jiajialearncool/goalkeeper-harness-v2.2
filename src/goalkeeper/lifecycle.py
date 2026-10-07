"""Finish owned, bounded cleanup even when another cancellation arrives."""
import asyncio


async def finish_cleanup(operation):
    task = asyncio.create_task(operation)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result
