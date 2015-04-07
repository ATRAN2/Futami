#! /usr/bin/env python
# Hey, Emacs! This is -*-python-*-.
#
# Copyright (C) 2003-2014 Joel Rosdahl
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
# USA
#
# Joel Rosdahl <joel@rosdahl.net>

import logging
import os
import re
import select
import ssl
import socket
import sys
import tempfile
import time
from datetime import datetime
from optparse import OptionParser


from futami.external.channel import Channel
from futami.external.client import Client
from futami.external.client import InternalClient

logger = logging.getLogger(__name__)

VERSION = "0.4"


def create_directory(path):
    if not os.path.isdir(path):
        os.makedirs(path)


class Server(object):
    def __init__(self, options):
        if options.debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        self.ports = options.ports
        self.password = options.password
        self.ssl_pem_file = options.ssl_pem_file
        self.motdfile = options.motd
        self.verbose = options.verbose
        self.debug = options.debug
        self.logdir = options.logdir
        self.chroot = options.chroot
        self.setuid = options.setuid
        self.statedir = options.statedir

        if options.listen:
            self.address = socket.gethostbyname(options.listen)
        else:
            self.address = ""
        self.name = socket.getfqdn(self.address)[:63]  # RFC 2813 2.1

        self.clients = {}  # Socket --> Client instance.
        self.nicknames = {}  # irc_lower(Nickname) --> Client instance.
        if self.logdir:
            create_directory(self.logdir)
        if self.statedir:
            create_directory(self.statedir)

        self.internal_client = InternalClient(self, 'control', 'ControlUser')

    def daemonize(self):
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError:
            sys.exit(1)
        os.setsid()
        try:
            pid = os.fork()
            if pid > 0:
                logger.info("PID: %d", pid)
                sys.exit(0)
        except OSError:
            sys.exit(1)
        os.chdir("/")
        os.umask(0)
        dev_null = open("/dev/null", "r+")
        os.dup2(dev_null.fileno(), sys.stdout.fileno())
        os.dup2(dev_null.fileno(), sys.stderr.fileno())
        os.dup2(dev_null.fileno(), sys.stdin.fileno())

    def get_client(self, nickname):
        return self.nicknames.get(irc_lower(nickname))

    def get_motd_lines(self):
        if self.motdfile:
            try:
                return open(self.motdfile).readlines()
            except IOError:
                return ["Could not read MOTD file %r." % self.motdfile]
        else:
            return []

    def client_changed_nickname(self, client, oldnickname):
        if oldnickname:
            del self.nicknames[irc_lower(oldnickname)]
        self.nicknames[irc_lower(client.nickname)] = client

    def remove_member_from_channel(self, client, channelname):
        if irc_lower(channelname) in self.channels:
            channel = self.channels[irc_lower(channelname)]
            channel.remove_client(client)

    def remove_client(self, client, quitmsg):
        client.message_related(":%s QUIT :%s" % (client.prefix, quitmsg))
        for x in list(client.channels.values()):
            client.channel_log(x, "quit (%s)" % quitmsg, meta=True)
            x.remove_client(client)
        if client.nickname \
                and irc_lower(client.nickname) in self.nicknames:
            del self.nicknames[irc_lower(client.nickname)]
        del self.clients[client.socket]

    def remove_channel(self, channel):
        del self.channels[irc_lower(channel.name)]

    def start(self):
        self.server_sockets = []
        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.address, port))
            except socket.error as e:
                logger.error("Could not bind port %s: %s.", port, e)
                sys.exit(1)
            s.listen(5)
            self.server_sockets.append(s)
            logger.info("Listening on port %d.", port)
        if self.chroot:
            os.chdir(self.chroot)
            os.chroot(self.chroot)
            logger.info("Changed root directory to %s", self.chroot)
        if self.setuid:
            os.setgid(self.setuid[1])
            os.setuid(self.setuid[0])
            logger.info("Setting uid:gid to %s:%s",
                        self.setuid[0], self.setuid[1])
        self.last_aliveness_check = time.time()

        self.run_loop()

    def run_loop(self):
        while True:
            queue_pseudo_socket = self.internal_client.response_queue._reader
            client_sockets = [
                client.socket
                for client in list(self.clients.values())
            ]
            (readable_sockets, writable_sockets, _) = select.select(
                self.server_sockets + client_sockets + [queue_pseudo_socket],
                [client.socket for client in list(self.clients.values())
                 if client.write_queue_size() > 0],
                [],
            )

            if queue_pseudo_socket in readable_sockets:
                self.internal_client.loop_hook()
                readable_sockets.remove(queue_pseudo_socket)

            for client in readable_sockets:
                if client in self.clients:
                    self.clients[client].socket_readable_notification()
                else:
                    (conn, addr) = client.accept()
                    if self.ssl_pem_file:
                        conn = self._maybe_wrap_ssl(conn, addr)
                    if not conn:
                        continue
                    self.clients[conn] = Client(self, conn)
                    logger.info("Accepted connection from %s:%s.",
                                addr[0], addr[1])

            for client in writable_sockets:
                if client in self.clients:  # client may have been disconnected
                    self.clients[client].socket_writable_notification()

            now = time.time()
            if self.last_aliveness_check + 10 < now:
                for client in list(self.clients.values()):
                    client.check_aliveness()
                self.last_aliveness_check = now

    def _maybe_wrap_ssl(self, conn, addr):
        try:
            return ssl.wrap_socket(
                conn,
                server_side=True,
                certfile=self.ssl_pem_file,
                keyfile=self.ssl_pem_file
            )
        except ssl.SSLError as e:
            logger.error(
                "SSL error for connection from %s:%s: %s",
                addr[0], addr[1], e)
            return None


_alpha = "abcdefghijklmnopqrstuvwxyz"
_ircstring_translation = str.maketrans(
    str.upper(_alpha) + "[]\\^",
    _alpha + "{}|~")


def irc_lower(s):
    return str.translate(s, _ircstring_translation)


def parse_options(argv):
    op = OptionParser(
        version=VERSION,
        description="miniircd is a small and limited IRC server.")
    op.add_option(
        "-d", "--daemon",
        action="store_true",
        help="fork and become a daemon")
    op.add_option(
        "--debug",
        action="store_true",
        help="print debug messages to stdout")
    op.add_option(
        "--listen",
        metavar="X",
        help="listen on specific IP address X")
    op.add_option(
        "--logdir",
        metavar="X",
        help="store channel log in directory X")
    op.add_option(
        "--motd",
        metavar="X",
        help="display file X as message of the day")
    op.add_option(
        "-s", "--ssl-pem-file",
        metavar="FILE",
        help="enable SSL and use FILE as the .pem certificate+key")
    op.add_option(
        "-p", "--password",
        metavar="X",
        help="require connection password X; default: no password")
    op.add_option(
        "--ports",
        metavar="X",
        help="listen to ports X (a list separated by comma or whitespace);"
             " default: 6667 or 6697 if SSL is enabled")
    op.add_option(
        "--statedir",
        metavar="X",
        help="save persistent channel state (topic, key) in directory X")
    op.add_option(
        "--verbose",
        action="store_true",
        help="be verbose (print some progress messages to stdout)")
    if os.name == "posix":
        op.add_option(
            "--chroot",
            metavar="X",
            help="change filesystem root to directory X after startup"
                 " (requires root)")
        op.add_option(
            "--setuid",
            metavar="U[:G]",
            help="change process user (and optionally group) after startup"
                 " (requires root)")

    (options, args) = op.parse_args(argv[1:])
    if options.debug:
        options.verbose = True
    if options.ports is None:
        if options.ssl_pem_file is None:
            options.ports = "6667"
        else:
            options.ports = "6697"
    if options.chroot:
        if os.getuid() != 0:
            op.error("Must be root to use --chroot")
    if options.setuid:
        from pwd import getpwnam
        from grp import getgrnam
        if os.getuid() != 0:
            op.error("Must be root to use --setuid")
        matches = options.setuid.split(":")
        if len(matches) == 2:
            options.setuid = (getpwnam(matches[0]).pw_uid,
                              getgrnam(matches[1]).gr_gid)
        elif len(matches) == 1:
            options.setuid = (getpwnam(matches[0]).pw_uid,
                              getpwnam(matches[0]).pw_gid)
        else:
            op.error("Specify a user, or user and group separated by a colon,"
                     " e.g. --setuid daemon, --setuid nobody:nobody")
    if (os.getuid() == 0 or os.getgid() == 0) and not options.setuid:
        op.error("Running this service as root is not recommended. Use the"
                 " --setuid option to switch to an unprivileged account after"
                 " startup. If you really intend to run as root, use"
                 " \"--setuid root\".")

    ports = []
    for port in re.split(r"[,\s]+", options.ports):
        try:
            ports.append(int(port))
        except ValueError:
            op.error("bad port: %r" % port)
    options.ports = ports
    return options
