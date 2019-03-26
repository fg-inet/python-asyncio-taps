#!/bin/bash

cat > ca.cnf << EOF
#
# Default configuration to use when one is not provided on the command line.
#
[ ca ]
default_ca = local_ca
#
#
# Default location of directories and files needed to generate certificates.
#
[ local_ca ]
dir = .
certificate = ca.cert
database = ca.index
new_certs_dir = .
private_key = ca.key
serial = ca.serial
#
#
# Default expiration and encryption policies for certificates
#
default_crl_days = 30
default_days = 30
# sha1 is no longer recommended, we will be using sha256
default_md = sha512
#
policy = local_ca_policy
x509_extensions = local_ca_extensions
#
#
# Copy extensions specified in the certificate request
#
copy_extensions = copy
#
#
# Default policy to use when generating server certificates.
# The following fields must be defined in the server certificate.
#
# DO NOT CHANGE "supplied" BELOW TO ANYTHING ELSE.
# It is the correct content.
#
[ local_ca_policy ]
commonName = supplied
organizationName = supplied
#
#
# x509 extensions to use when generating server certificates
#
[ local_ca_extensions ]
basicConstraints = CA:false
#
#
# The default root certificate generation policy
#
[ req ]
default_bits = 4096
default_keyfile = ca.key
#
# sha1 is no longer recommended, we will be using sha256
default_md = sha512
#
prompt = no
distinguished_name = root_ca_distinguished_name
x509_extensions = root_ca_extensions
#
#
# Root Certificate Authority distinguished name
#
# DO CHANGE THE CONTENT OF THESE FIELDS TO MATCH
# YOUR OWN SETTINGS!
#
[ root_ca_distinguished_name ]
commonName = PyTAPS Test CA
emailAddress = pytapsca@invalid.
#
[ root_ca_extensions ]
basicConstraints = CA:true

EOF

export OPENSSL_CONF=$(pwd)/ca.cnf
openssl req -x509 -newkey rsa:4096 -out ca.cert -outform PEM -days 90 -nodes

touch ca.index
echo '01' > ca.serial

cat > localhost.cnf <<EOF
#
# localhost.cnf
#

[ req ]
prompt = no
distinguished_name = server_distinguished_name
req_extensions = v3_req

[ server_distinguished_name ]
commonName = localhost
organizationName = PyTAPS
organizationalUnitName = Testing

[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
subjectAltName = @alt_names

[ alt_names ]
DNS.0 = localhost

EOF

export OPENSSL_CONF=$(pwd)/localhost.cnf
openssl req -newkey rsa:4096 -keyout server.key -keyform PEM -out server.req -outform PEM -nodes

export OPENSSL_CONF=$(pwd)/ca.cnf
openssl ca -in server.req -out server.crt

cat server.crt >> server.key
