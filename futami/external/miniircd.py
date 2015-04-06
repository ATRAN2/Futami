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
from multiprocessing import Queue

from futami.ami import Ami

VERSION = "0.4"


logger = logging.getLogger(__name__)


def create_directory(path):
    if not os.path.isdir(path):
        os.makedirs(path)


class Channel(object):
    def __init__(self, server, name):
        self.server = server
        self.name = name
        self.members = set()
        self._topic = ""
        self._key = None
        if self.server.statedir:
            self._state_path = "%s/%s" % (
                self.server.statedir,
                name.replace("_", "__").replace("/", "_"))
            self._read_state()
        else:
            self._state_path = None

    def add_member(self, client):
        self.members.add(client)

    def get_topic(self):
        return self._topic

    def set_topic(self, value):
        self._topic = value
        self._write_state()

    topic = property(get_topic, set_topic)

    def get_key(self):
        return self._key

    def set_key(self, value):
        self._key = value
        self._write_state()

    key = property(get_key, set_key)

    def remove_client(self, client):
        self.members.discard(client)
        if not self.members:
            self.server.remove_channel(self)

    def _read_state(self):
        if not (self._state_path and os.path.exists(self._state_path)):
            return
        data = {}
        with open(self._state_path) as f:
            exec(f.read(), {}, data)
        self._topic = data.get("topic", "")
        self._key = data.get("key")

    def _write_state(self):
        if not self._state_path:
            return
        (fd, path) = tempfile.mkstemp(dir=os.path.dirname(self._state_path))
        fp = os.fdopen(fd, "w")
        fp.write("topic = %r\n" % self.topic)
        fp.write("key = %r\n" % self.key)
        fp.close()
        os.rename(path, self._state_path)


    def __repr__(self):
        return "<{} {}>".format(self.__class__.__name__, self.name)

class Client(object):
    __linesep_regexp = re.compile(r"\r?\n")
    # The RFC limit for nicknames is 9 characters, but what the heck.
    __valid_nickname_regexp = re.compile(
        r"^[][\`_^{|}A-Za-z][][\`_^{|}A-Za-z0-9]{0,50}$")
    __valid_channelname_regexp = re.compile(
        r"^[&#+!][^\x00\x07\x0a\x0d ,:]{0,50}$")

    def __init__(self, server, socket):
        self.server = server
        self.socket = socket
        self.channels = {}  # irc_lower(Channel name) --> Channel
        self.nickname = None
        self.user = None
        self.realname = None
        (self.host, self.port) = socket.getpeername()
        self.__timestamp = time.time()
        self._readbuffer = ""
        self._writebuffer = ""
        self.__sent_ping = False
        if self.server.password:
            self._handle_command = self.__pass_handler
        else:
            self._handle_command = self.__registration_handler

    @property
    def prefix(self):
        return "%s!%s@%s" % (self.nickname, self.user, self.host)

    def check_aliveness(self):
        now = time.time()
        if self.__timestamp + 180 < now:
            self.disconnect("ping timeout")
            return
        if not self.__sent_ping and self.__timestamp + 90 < now:
            if self._handle_command == self.__command_handler:
                # Registered.
                self.message("PING :%s" % self.server.name)
                self.__sent_ping = True
            else:
                # Not registered.
                self.disconnect("ping timeout")

    def write_queue_size(self):
        return len(self._writebuffer)

    def _parse_read_buffer(self):
        lines = self.__linesep_regexp.split(self._readbuffer)
        self._readbuffer = lines[-1]
        lines = lines[:-1]
        for line in lines:
            if not line:
                # Empty line. Ignore.
                continue
            x = line.split(" ", 1)
            command = x[0].upper()
            if len(x) == 1:
                arguments = []
            else:
                if len(x[1]) > 0 and x[1][0] == ":":
                    arguments = [x[1][1:]]
                else:
                    y = str.split(x[1], " :", 1)
                    arguments = str.split(y[0])
                    if len(y) == 2:
                        arguments.append(y[1])
            self._handle_command(command, arguments)

    def __pass_handler(self, command, arguments):
        server = self.server
        if command == "PASS":
            if len(arguments) == 0:
                self.reply_461("PASS")
            else:
                if arguments[0].lower() == server.password:
                    self._handle_command = self.__registration_handler
                else:
                    self.reply("464 :Password incorrect")
        elif command == "QUIT":
            self.disconnect("Client quit")
            return

    def __registration_handler(self, command, arguments):
        server = self.server
        if command == "NICK":
            if len(arguments) < 1:
                self.reply("431 :No nickname given")
                return
            nick = arguments[0]
            if server.get_client(nick):
                self.reply("433 * %s :Nickname is already in use" % nick)
            elif not self.__valid_nickname_regexp.match(nick):
                self.reply("432 * %s :Erroneous nickname" % nick)
            else:
                self.nickname = nick
                server.client_changed_nickname(self, None)
        elif command == "USER":
            if len(arguments) < 4:
                self.reply_461("USER")
                return
            self.user = arguments[0]
            self.realname = arguments[3]
        elif command == "QUIT":
            self.disconnect("Client quit")
            return
        if self.nickname and self.user:
            self.reply("001 %s :Hi, welcome to IRC" % self.nickname)
            self.reply("002 %s :Your host is %s, running version miniircd-%s"
                       % (self.nickname, server.name, VERSION))
            self.reply("003 %s :This server was created sometime"
                       % self.nickname)
            self.reply("004 %s :%s miniircd-%s o o"
                       % (self.nickname, server.name, VERSION))
            self.send_lusers()
            self.send_motd()
            self._handle_command = self.__command_handler

    def __command_handler(self, command, arguments):
        def away_handler():
            pass

        def ison_handler():
            if len(arguments) < 1:
                self.reply_461("ISON")
                return
            nicks = arguments
            online = [n for n in nicks if server.get_client(n)]
            self.reply("303 %s :%s" % (self.nickname, " ".join(online)))

        def join_handler():
            if len(arguments) < 1:
                self.reply_461("JOIN")
                return
            if arguments[0] == "0":
                for (channelname, channel) in list(self.channels.items()):
                    self.message_channel(channel, "PART", channelname, True)
                    self.channel_log(channel, "left", meta=True)
                    server.remove_member_from_channel(self, channelname)
                self.channels = {}
                return
            channelnames = arguments[0].split(",")
            if len(arguments) > 1:
                keys = arguments[1].split(",")
            else:
                keys = []
            keys.extend((len(channelnames) - len(keys)) * [None])
            for (i, channelname) in enumerate(channelnames):
                if irc_lower(channelname) in self.channels:
                    continue
                if not valid_channel_re.match(channelname):
                    self.reply_403(channelname)
                    continue
                channel = Channel(self.server, channelname)
                if channel.key is not None and channel.key != keys[i]:
                    self.reply(
                        "475 %s %s :Cannot join channel (+k) - bad key"
                        % (self.nickname, channelname))
                    continue

                channel.add_member(self)
                self.channels[irc_lower(channelname)] = channel
                self.message_channel(channel, "JOIN", channelname, True)
                self.channel_log(channel, "joined", meta=True)
                if channel.topic:
                    self.reply("332 %s %s :%s"
                               % (self.nickname, channel.name, channel.topic))
                else:
                    self.reply("331 %s %s :No topic is set"
                               % (self.nickname, channel.name))
                self.reply("353 %s = %s :%s"
                           % (self.nickname,
                              channelname,
                              " ".join(sorted(x.nickname
                                              for x in channel.members))))
                self.reply("366 %s %s :End of NAMES list"
                           % (self.nickname, channelname))

                # Add the internal client first so he sees the join..
                # You'd think it would make sense to add the internal client
                # prior and let it handle the join,
                # but what ends up happening is that it processes the join
                # and sends a welcome privmsg before we even finish the
                # bookkeeping, confusing the client. Informing the
                # internal client should really happen here, and if we can't
                # act on the standard join we might as well alert the internal
                # client separately.
                channel.add_member(self.server.internal_client)
                self.server.internal_client.client_joined(self, channel)



        def list_handler():

            if len(arguments) < 1:
                channels = list(self.channels.values())
            else:
                channels = []
                for channelname in arguments[0].split(","):
                    if channelname in self.channels:
                        channels.append(self.channels[channelname])
            channels.sort(key=lambda x: x.name)
            for channel in channels:
                self.reply("322 %s %s %d :%s"
                           % (self.nickname, channel.name,
                              len(channel.members), channel.topic))
            self.reply("323 %s :End of LIST" % self.nickname)

        def lusers_handler():
            self.send_lusers()

        def mode_handler():
            if len(arguments) < 1:
                self.reply_461("MODE")
                return
            targetname = arguments[0]
            if targetname in self.channels:
                channel = self.channels[targetname]
                if len(arguments) < 2:
                    if channel.key:
                        modes = "+k"
                        if irc_lower(channel.name) in self.channels:
                            modes += " %s" % channel.key
                    else:
                        modes = "+"
                    self.reply("324 %s %s %s"
                               % (self.nickname, targetname, modes))
                    return
                flag = arguments[1]
                if flag == "+k":
                    if len(arguments) < 3:
                        self.reply_461("MODE")
                        return
                    key = arguments[2]
                    if irc_lower(channel.name) in self.channels:
                        channel.key = key
                        self.message_channel(
                            channel, "MODE", "%s +k %s" % (channel.name, key),
                            True)
                        self.channel_log(
                            channel, "set channel key to %s" % key, meta=True)
                    else:
                        self.reply("442 %s :You're not on that channel"
                                   % targetname)
                elif flag == "-k":
                    if irc_lower(channel.name) in self.channels:
                        channel.key = None
                        self.message_channel(
                            channel, "MODE", "%s -k" % channel.name,
                            True)
                        self.channel_log(
                            channel, "removed channel key", meta=True)
                    else:
                        self.reply("442 %s :You're not on that channel"
                                   % targetname)
                else:
                    self.reply("472 %s %s :Unknown MODE flag"
                               % (self.nickname, flag))
            elif targetname == self.nickname:
                if len(arguments) == 1:
                    self.reply("221 %s +" % self.nickname)
                else:
                    self.reply("501 %s :Unknown MODE flag" % self.nickname)
            else:
                self.reply_403(targetname)

        def motd_handler():
            self.send_motd()

        def nick_handler():
            if len(arguments) < 1:
                self.reply("431 :No nickname given")
                return
            newnick = arguments[0]
            client = server.get_client(newnick)
            if newnick == self.nickname:
                pass
            elif client and client is not self:
                self.reply("433 %s %s :Nickname is already in use"
                           % (self.nickname, newnick))
            elif not self.__valid_nickname_regexp.match(newnick):
                self.reply("432 %s %s :Erroneous Nickname"
                           % (self.nickname, newnick))
            else:
                for x in list(self.channels.values()):
                    self.channel_log(
                        x, "changed nickname to %s" % newnick, meta=True)
                oldnickname = self.nickname
                self.nickname = newnick
                server.client_changed_nickname(self, oldnickname)
                self.message_related(
                    ":%s!%s@%s NICK %s"
                    % (oldnickname, self.user, self.host, self.nickname),
                    True)

        def notice_and_privmsg_handler():
            if len(arguments) == 0:
                self.reply("411 %s :No recipient given (%s)"
                           % (self.nickname, command))
                return
            if len(arguments) == 1:
                self.reply("412 %s :No text to send" % self.nickname)
                return
            targetname = arguments[0]
            message = arguments[1]
            client = server.get_client(targetname)
            if client:
                client.message(":%s %s %s :%s"
                               % (self.prefix, command, targetname, message))
            elif targetname in self.channels:
                channel = self.channels[targetname]
                self.message_channel(
                    channel, command, "%s :%s" % (channel.name, message))
                self.channel_log(channel, message)
            else:
                self.reply("401 %s %s :No such nick/channel"
                           % (self.nickname, targetname))

        def part_handler():
            if len(arguments) < 1:
                self.reply_461("PART")
                return
            if len(arguments) > 1:
                partmsg = arguments[1]
            else:
                partmsg = self.nickname
            for channelname in arguments[0].split(","):
                if not valid_channel_re.match(channelname):
                    self.reply_403(channelname)
                elif not irc_lower(channelname) in self.channels:
                    self.reply("442 %s %s :You're not on that channel"
                               % (self.nickname, channelname))
                else:
                    channel = self.channels[irc_lower(channelname)]
                    self.message_channel(
                        channel, "PART", "%s :%s" % (channelname, partmsg),
                        True)
                    self.channel_log(channel, "left (%s)" % partmsg, meta=True)
                    del self.channels[irc_lower(channelname)]
                    server.remove_member_from_channel(self, channelname)

        def ping_handler():
            if len(arguments) < 1:
                self.reply("409 %s :No origin specified" % self.nickname)
                return
            self.reply("PONG %s :%s" % (server.name, arguments[0]))

        def pong_handler():
            pass

        def quit_handler():
            if len(arguments) < 1:
                quitmsg = self.nickname
            else:
                quitmsg = arguments[0]
            self.disconnect(quitmsg)

        def topic_handler():
            if len(arguments) < 1:
                self.reply_461("TOPIC")
                return
            channelname = arguments[0]
            channel = self.channels.get(irc_lower(channelname))
            if channel:
                if len(arguments) > 1:
                    newtopic = arguments[1]
                    channel.topic = newtopic
                    self.message_channel(
                        channel, "TOPIC", "%s :%s" % (channelname, newtopic),
                        True)
                    self.channel_log(
                        channel, "set topic to %r" % newtopic, meta=True)
                else:
                    if channel.topic:
                        self.reply("332 %s %s :%s"
                                   % (self.nickname, channel.name,
                                      channel.topic))
                    else:
                        self.reply("331 %s %s :No topic is set"
                                   % (self.nickname, channel.name))
            else:
                self.reply("442 %s :You're not on that channel" % channelname)

        def wallops_handler():
            if len(arguments) < 1:
                self.reply_461(command)
            message = arguments[0]
            for client in list(server.clients.values()):
                client.message(":%s NOTICE %s :Global notice: %s"
                               % (self.prefix, client.nickname, message))

        def who_handler():
            if len(arguments) < 1:
                return
            targetname = arguments[0]
            if targetname in self.channels:
                channel = self.channels[targetname]
                for member in channel.members:
                    self.reply("352 %s %s %s %s %s %s H :0 %s"
                               % (self.nickname, targetname, member.user,
                                  member.host, server.name, member.nickname,
                                  member.realname))
                self.reply("315 %s %s :End of WHO list"
                           % (self.nickname, targetname))

        def whois_handler():
            if len(arguments) < 1:
                return
            username = arguments[0]
            user = server.get_client(username)
            if user:
                self.reply("311 %s %s %s %s * :%s"
                           % (self.nickname, user.nickname, user.user,
                              user.host, user.realname))
                self.reply("312 %s %s %s :%s"
                           % (self.nickname, user.nickname, server.name,
                              server.name))
                self.reply("319 %s %s :%s"
                           % (self.nickname, user.nickname,
                              " ".join(user.channels)))
                self.reply("318 %s %s :End of WHOIS list"
                           % (self.nickname, user.nickname))
            else:
                self.reply("401 %s %s :No such nick"
                           % (self.nickname, username))

        handler_table = {
            "AWAY": away_handler,
            "ISON": ison_handler,
            "JOIN": join_handler,
            "LIST": list_handler,
            "LUSERS": lusers_handler,
            "MODE": mode_handler,
            "MOTD": motd_handler,
            "NICK": nick_handler,
            "NOTICE": notice_and_privmsg_handler,
            "PART": part_handler,
            "PING": ping_handler,
            "PONG": pong_handler,
            "PRIVMSG": notice_and_privmsg_handler,
            "QUIT": quit_handler,
            "TOPIC": topic_handler,
            "WALLOPS": wallops_handler,
            "WHO": who_handler,
            "WHOIS": whois_handler,
        }
        server = self.server
        valid_channel_re = self.__valid_channelname_regexp
        try:
            handler_table[command]()
        except KeyError:
            self.reply("421 %s %s :Unknown command" % (self.nickname, command))

    def socket_readable_notification(self):
        try:
            data = self.socket.recv(2 ** 10).decode('utf-8')
            logger.debug('[%s:%d] -> %r', self.host, self.port, data)
            quitmsg = "EOT"
        except socket.error as x:
            data = ""
            quitmsg = x
        except UnicodeDecodeError as x:
            return
        if data:
            self._readbuffer += data
            self._parse_read_buffer()
            self.__timestamp = time.time()
            self.__sent_ping = False
        else:
            self.disconnect(quitmsg)

    def socket_writable_notification(self):
        try:
            sent = self.socket.send(self._writebuffer.encode('utf-8'))
            logger.debug('[%s:%d] <- %r',
                         self.host, self.port, self._writebuffer[:sent])
            self._writebuffer = self._writebuffer[sent:]
        except socket.error as x:
            self.disconnect(x)

    def disconnect(self, quitmsg):
        self.message("ERROR :%s" % quitmsg)
        logger.info(
            "Disconnected connection from %s:%s (%s).",
            self.host, self.port, quitmsg)
        self.socket.close()
        self.server.remove_client(self, quitmsg)

    def message(self, msg):
        self._writebuffer += msg + "\r\n"

    def reply(self, msg):
        self.message(":%s %s" % (self.server.name, msg))

    def reply_403(self, channel):
        self.reply("403 %s %s :No such channel" % (self.nickname, channel))

    def reply_461(self, command):
        nickname = self.nickname or "*"
        self.reply("461 %s %s :Not enough parameters" % (nickname, command))

    def message_channel(self, channel, command, message, include_self=False):
        line = ":%s %s %s" % (self.prefix, command, message)
        print(channel.members)
        for client in channel.members:
            print(self, client, client != self)
            if client != self or include_self:
                print("Messaging", client)
                client.message(line)

    def channel_log(self, channel, message, meta=False):
        if not self.server.logdir:
            return
        if meta:
            format = "[%s] * %s %s\n"
        else:
            format = "[%s] <%s> %s\n"
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        logname = channel.name.replace("_", "__").replace("/", "_")
        fp = open("%s/%s.log" % (self.server.logdir, logname), "a")
        fp.write(format % (timestamp, self.nickname, message))
        fp.close()

    def message_related(self, msg, include_self=False):
        clients = set()
        if include_self:
            clients.add(self)
        for channel in list(self.channels.values()):
            clients |= channel.members
        if not include_self:
            clients.discard(self)
        for client in clients:
            client.message(msg)

    def send_lusers(self):
        self.reply("251 %s :There are %d users and 0 services on 1 server"
                   % (self.nickname, len(self.server.clients)))

    def send_motd(self):
        server = self.server
        motdlines = server.get_motd_lines()
        if motdlines:
            self.reply("375 %s :- %s Message of the day -"
                       % (self.nickname, server.name))
            for line in motdlines:
                self.reply("372 %s :- %s" % (self.nickname, line.rstrip()))
            self.reply("376 %s :End of /MOTD command" % self.nickname)
        else:
            self.reply("422 %s :MOTD File is missing" % self.nickname)

    def __repr__(self):
        return "<{} {}>".format(self.__class__.__name__, self.prefix)

class InternalClient(Client):
    def __init__(self, server, nickname, user, host='localhost'):
        self.server = server
        self.nickname = nickname
        self.realname = nickname
        self.user = user
        self.host = host

        self._readbuffer = ""
        self._writebuffer = ""
        self.update_queue = Queue()
        self.update_agent = Ami(self.update_queue)

    def loop_hook(self):
        while not self.update_queue.empty():
            result = self.update_queue.get()

    def _parse_prefix(self, prefix):
        m = re.search(":(?P<nickname>[^!]*)!(?P<username>[^@]*)@(?P<host>.*)", prefix)
        return m.groupdict()

    @property
    def socket(self):
        raise AttributeError('InternalClients have no sockets')

    def message(self, message):
        prefix, message = message.split(" ", 1)

        prefix = self._parse_prefix(prefix)

        self.sending_client = self.server.get_client(prefix['nickname'])

        self._readbuffer = message + '\r\n'
        self._parse_read_buffer()

    def _handle_command(self, command, arguments):
        sending_client = self.sending_client
        self.sending_client = None

        # Add handling here for handling actual input from users other than
        # joins

    def client_joined(self, client, channel):
        self._send_message(client, channel.name, "Welcome to {}".format(channel.name))

        channel_name = channel.name[1:]

        if not (channel_name.startswith('/') and channel_name.endswith('/')):
            self._send_message(client, channel.name, "This doesn't look like a board. Nothing will happen in this channel.")
            return

    def _send_message(self, client, channel, message, sending_nick=None):
        if sending_nick:
            real_nick = self.nickname
            self.nickname = sending_nick

        client.message(
            ":{} PRIVMSG {} :{}".format(
                self.prefix,
                channel,
                message,
        ))

        if sending_nick:
            self.nickname = real_nick

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
            self.internal_client.loop_hook()

            client_sockets = [client.socket for client in list(self.clients.values())]
            (readable_sockets, writable_sockets, _) = select.select(
                self.server_sockets + client_sockets,
                [client.socket for client in list(self.clients.values())
                 if client.write_queue_size() > 0],
                [],
                10
            )

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
