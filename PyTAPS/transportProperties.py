from enum import Enum


class preferenceLevel(Enum):
    REQUIRE = 2
    PREFER = 1
    IGNORE = 0
    AVOID = -1
    PROHIBIT = -2


class transportProperties:
    """ Class to handle the TAPS transport properties

    """
    def __init__(self):
        self.properties = {"Reliable_Data_Transfer": preferenceLevel.IGNORE}

    def add(self, prop, value):
        self.properties[prop] = value

    def require(self, prop):
        self.properties[prop] = preferenceLevel.REQUIRE

    def prefer(self, prop):
        self.properties[prop] = preferenceLevel.PREFER

    def ignore(self, prop):
        self.properties[prop] = preferenceLevel.IGNORE

    def avoid(self, prop):
        self.properties[prop] = preferenceLevel.AVOID

    def prohibit(self, prop):
        self.properties[prop] = preferenceLevel.PROHIBIT
