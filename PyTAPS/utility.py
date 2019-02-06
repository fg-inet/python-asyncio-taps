import datetime
from termcolor import colored


def printTime(msg="", color="red"):
    print(colored(str(datetime.datetime.now())+": "+msg, color))
