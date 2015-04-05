import logging
import sys

from futami.external import miniircd


class AmiServer(miniircd.Server):
    pass


def main():
    logging.basicConfig(
        format='%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s',
    )
    options = miniircd.parse_options(sys.argv)
    server = AmiServer(options)
    if options.daemon:
        server.daemonize()

    try:
        server.start()
    except KeyboardInterrupt:
        server.print_error("Interrupted.")
