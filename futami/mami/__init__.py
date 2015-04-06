import logging
import sys

from futami.external import miniircd


class MamiServer(miniircd.Server):
    pass


def main():
    logging.basicConfig(
        format=('%(asctime)s %(process)d %(module)s '
                '[%(levelname)s] %(message)s'),
    )
    options = miniircd.parse_options(sys.argv)
    server = MamiServer(options)
    if options.daemon:
        server.daemonize()

    try:
        server.start()
    except KeyboardInterrupt:
        logging.error("Interrupted.")
