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
