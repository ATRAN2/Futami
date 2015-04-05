import sys

from futami.external import miniircd


class AmiServer(miniircd.Server):
    pass


def main():
    options = miniircd.parse_options(sys.argv)
    server = AmiServer(options)
    if options.daemon:
        server.daemonize()

    try:
        server.start()
    except KeyboardInterrupt:
        server.print_error("Interrupted.")
