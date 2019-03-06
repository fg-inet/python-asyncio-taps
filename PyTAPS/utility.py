import datetime
from termcolor import colored


def print_time(msg="", color="red"):
    print(colored(str(datetime.datetime.now())+": "+msg, color))
