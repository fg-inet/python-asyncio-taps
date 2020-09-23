import asyncio
import datetime
import logging
import warnings
from enum import Enum

from pytaps.transportProperties import get_protocols, PreferenceLevel

colors = {
    "red": "\x1b[31;1m",
    "green": "\x1b[32;1m",
    "yellow": "\x1b[33;1m",
    "blue": "\x1b[34;1m",
    "magenta": "\x1b[35;1m",
    "cyan": "\x1b[36;1m",
    "grey": "\x1b[37;1m",
    "white": "\x1b[38;1m"
}


class ConnectionState(Enum):
    ESTABLISHING = 0
    ESTABLISHED = 1
    CLOSING = 2
    CLOSED = 3


def print_time(msg="", color="red"):
    warnings.warn("\x1b[31;1mprint_time is deprecated, switch to a logger (e.g. with setup_logger()).\x1b[0m", DeprecationWarning, 2)
    print(str(datetime.datetime.now()) + ": " + msg, color)


def color_emit(emit):
    def new(*args):
        level = args[0].levelno
        if level == logging.CRITICAL:
            color = "red"
        elif level == logging.WARNING:
            color = "yellow"
        elif level == logging.INFO:
            color = "green"
        else:
            color = "white"
        args[0].levelname = f'{colors[color]}{args[0].levelname}\x1b[0m'
        return emit(*args)
    return new


def setup_logger(module, color="white"):
    logger = logging.getLogger(module)
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(f'%(asctime)s - {colors[color]}%(name)s \x1b[0m- %(levelname)s: %(message)s'))
    ch.emit = color_emit(ch.emit)
    logger.addHandler(ch)
    return logger


def create_candidates(connection):
    """ Decides which protocols are candidates and then orders them
    according to the TAPS interface draft
    """
    # Get the protocols know to the implementation from transportProperties
    available_protocols = get_protocols()

    # At the beginning, all protocols are candidates
    candidate_protocols = dict([(row["name"], list((0, 0)))
                                for row in available_protocols])

    # Iterate over all available protocols and over all properties
    for protocol in available_protocols:
        for transport_property in connection.transport_properties.properties:
            # If a protocol has a prohibited property remove it
            if (connection.transport_properties.properties[transport_property]
                    is PreferenceLevel.PROHIBIT):
                if (protocol[transport_property] is True and
                        protocol["name"] in candidate_protocols):
                    del candidate_protocols[protocol["name"]]
            # If a protocol doesnt have a required property remove it
            if (connection.transport_properties.properties[transport_property]
                    is PreferenceLevel.REQUIRE):
                if (protocol[transport_property] is False and
                        protocol["name"] in candidate_protocols):
                    del candidate_protocols[protocol["name"]]
            # Count how many PREFER properties each protocol has
            if (connection.transport_properties.properties[transport_property]
                    is PreferenceLevel.PREFER):
                if (protocol[transport_property] is True and
                        protocol["name"] in candidate_protocols):
                    candidate_protocols[protocol["name"]][0] += 1
            # Count how many AVOID properties each protocol has
            if (connection.transport_properties.properties[transport_property]
                    is PreferenceLevel.AVOID):
                if (protocol[transport_property] is True and
                        protocol["name"] in candidate_protocols):
                    candidate_protocols[protocol["name"]][1] -= 1

    # Sort candidates by number of PREFERs and then by AVOIDs on ties
    sorted_candidates = sorted(candidate_protocols.items(),
                               key=lambda value: (value[1][0],
                                                  value[1][1]), reverse=True)

    return sorted_candidates


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
