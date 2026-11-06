import json


async def read_limited(response, limit):
    chunks, total = [], 0
    async for chunk in response.content.iter_chunked(65536):
        total += len(chunk)
        if total > limit:
            raise ValueError("响应超过允许大小")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json(response):
    return json.loads(await read_limited(response, 65536))
