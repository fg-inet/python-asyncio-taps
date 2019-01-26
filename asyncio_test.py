import asyncio
import random


async def get_int(a):
    return random.randint(a, a*a)

async def main(x, y):
    coroutines = [get_int(k) for k in range(x*y)]
    completed, pending = await asyncio.wait(coroutines)
    for a in completed:
        print(a.result())

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(1, 5))
    finally:
        loop.close()
