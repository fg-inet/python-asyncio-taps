from .utility import *

color = "magenta"


class SecurityParameters:
    """ Class to handle the TAPS security parameters.

    """

    def __init__(self):
        self.identity = None
        self.trustedCA = []

    def add_identity(self, identity):
        """ Adds a local identity with which to
            prove ones identity to a remote.
        Attributes:
            identity (string, required): Identity to be added.
        """
        if isinstance(identity, list):
            self.identity = identity[0]
        else:
            self.identity = identity
        print_time("Our certificate: " + str(self.identity), color)

    def add_trust_ca(self, cert):
        """ Adds a certificate to be trusted.
        Attributes:
            cert (string, required):  Certificate to be trusted.
        """
        self.trustedCA.append(cert)
        print_time("Trusting certificate: " + str(cert), color)
