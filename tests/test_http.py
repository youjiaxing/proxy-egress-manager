import pytest
from scheduler.http import read_limited


class Response:
    @property
    def content(self):
        return self
    async def iter_chunked(self, size):
        yield b'first chunk'
        yield b' and last chunk'


@pytest.mark.asyncio
async def test_bounded_read_waits_for_all_chunks_and_rejects_overflow():
    assert await read_limited(Response(),100)==b'first chunk and last chunk'
    with pytest.raises(ValueError):
        await read_limited(Response(),12)
