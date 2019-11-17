import asyncio
import datetime
from enum import Enum
from termcolor import colored


class ConnectionState(Enum):
    ESTABLISHING = 0
    ESTABLISHED = 1
    CLOSING = 2
    CLOSED = 3


def print_time(msg="", color="red"):
    print(colored(str(datetime.datetime.now())+": "+msg, color))

# Define our own sleep function which keeps track of its running calls
# so we can cancel them once the Connection is established
# https://stackoverflow.com/questions/37209864/interrupt-all-asyncio-sleep-currently-executing
class SleepClassForRacing:
    tasks = set()

    async def sleep(self, delay, result=None, *, loop=None):
        coro = asyncio.sleep(delay, result=result, loop=loop)
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            return result
        finally:
            self.tasks.remove(task)

    def cancel_all(self):
        for task in self.tasks:
            task.cancel()
