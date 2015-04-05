import logging
import sys

from futami.external import miniircd


class AmiServer(miniircd.Server):
    pass


def main():
    logging.basicConfig(
        format=('%(asctime)s %(process)d %(module)s '
                '[%(levelname)s] %(message)s'),
    )
    options = miniircd.parse_options(sys.argv)
    server = AmiServer(options)
    if options.daemon:
        server.daemonize()

    try:
        server.start()
    except KeyboardInterrupt:
        logging.error("Interrupted.")
