import asyncio
import copy
import time

from ..cost import valid_usage
from ..provider import ProviderError
from ..transport import MeteredTransport
from .diagnostics import error_details


class RetryProvider:
    """Buffer transient attempts; never replay a partially executed tool response."""
    def __init__(self, provider):
        self.provider = provider
        self.engine = None

    async def stream(self, request):
        outer = self.engine.state.calls[-1]
        for attempt in range(self.engine.state.config.transient_retries + 1):
            buffer, status, problem = [], None, None
            try:
                async for event in self.provider.stream(request):
                    if event.type in {'response.output_text.delta', 'response.output_item.added',
                                      'response.function_call_arguments.delta'}:
                        yield event
                        continue
                    if event.type == 'provider_http_error':
                        status = event.data.get('http_status')
                    buffer.append(event)
                for event in buffer:
                    yield event
                return
            except ProviderError as error:
                diagnostic = error_details(error, buffer)
                error.diagnostic = diagnostic
                outer['error_detail'] = diagnostic
                outer['error_reference'] = self.engine.store.archive(diagnostic)
                problem = {'error': {'code': error.code}}
                transient = (error.code in {'transport_error', 'interrupted_stream'}
                             or MeteredTransport.transient_http(problem, status))
                delay = 2 ** attempt
                if not transient or attempt == self.engine.state.config.transient_retries or self.engine.remaining() <= delay + 10:
                    self.engine.emit('response_discarded', {'reason': error.code})
                    for event in buffer:
                        if event.type in {'response.failed', 'response.incomplete'}:
                            yield event
                    raise
                failed = copy.deepcopy(outer)
                failed.update(status='failed', error=error.code, finished_at=time.time(), retry_attempt=attempt)
                failed.pop('usage', None)
                for event in buffer:
                    terminal = event.data.get('response', {})
                    if valid_usage(terminal.get('usage')):
                        failed['usage'] = terminal['usage']
                self.engine.state.calls.append(failed)
                # Engine owns the original record, independent of list ordering.
                outer.pop('error_detail', None)
                outer.pop('error_reference', None)
                outer['started_at'] = time.time() + delay
                self.engine.save('transient_retry', {'reason': error.code, 'attempt': attempt + 1})
                self.engine.emit('response_discarded', {'reason': error.code})
                await asyncio.sleep(delay)

    async def close(self):
        await self.provider.close()
