from .utility import *
color = "magenta"


class SecurityParameters:
    """ Class to handle the TAPS security parameters

    """
    def __init__(self):
        self.identity = None
        self.trustedCA = []

    def addIdentity(self, identity):
        if isinstance(identity, list):
            self.identity = identity[0]
        else:
            self.identity = identity
        print_time("Our certificate: " + str(self.identity), color)

    def addTrustCA(self, cert):
        self.trustedCA.append(cert)
        print_time("Trusting certificate: " + str(cert), color)
