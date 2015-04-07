# -*- coding: utf-8 -*-

from collections import namedtuple
import enum

SubscriptionUpdate = namedtuple('SubscriptionUpdate', ['action', 'target'])

BoardTarget = namedtuple('BoardTarget', ['board'])

ThreadTarget = namedtuple('ThreadTarget', ['board', 'thread'])


class Action(enum.Enum):
    LoadAndFollow = 1
    Stop = 2

    InternalQueueUpdate = 100


class Image(namedtuple('Image', ['filename', 'tim', 'ext', 'fsize', 'md5', 'w',
                                 'h', 'tn_w', 'tn_h', 'board'])):
    IMAGE = "https://i.4cdn.org/{board}/src/{tim}{ext}"
    IMAGE_THUMB = "https://t.4cdn.org/{board}/thumb/{tim}s.jpg"

    @property
    def image_url(self):
        return

    @property
    def thumb_url(self):
        return

    @property
    def filesize(self):
        return self.fsize

    def __repr__(self):
        return "<Image {0}{1} ({2}x{3})>".format(self.filename, self.ext,
                                                 self.w, self.h)


class Post:
    # Because moot is bad at updating the API documentation, the second
    # set of fields are not listed in the API documentation.
    # The third set of fields are values that are synthesized by us.
    post_fields = [
        'no', 'resto', 'sticky', 'closed', 'now', 'time', 'name', 'trip',
        'filename', 'id', 'capcode', 'country', 'country_name', 'email', 'sub',
        'com', 'tim', 'ext', 'fsize', 'md5', 'w', 'h', 'tn_w', 'tn_h',
        'filedeleted', 'spoiler', 'custom_spoiler', 'omitted_posts',
        'omitted_images', 'replies', 'images', 'bumplimit', 'imagelimit',
        'capcode_replies', 'last_modified', 'tag', 'semantic_url',

        'unique_ips',

        'board',
    ]

    interface_field_map = {
        'post_no': 'no',
        'reply_to': 'resto',
        'poster_name': 'name',
        'tripcode': 'trip',
        'id': 'id',
        'subject': 'sub',
        'comment': 'com',
        'board': 'board',
    }

    def __init__(self, data):
        missing_fields = set(self.post_fields).difference(data.keys())
        data.update({field: None for field in missing_fields})

        self.data = data

        self._image = Image(
            self.data['filename'],
            self.data['tim'],
            self.data['ext'],
            self.data['fsize'],
            self.data['md5'],
            self.data['w'],
            self.data['h'],
            self.data['tn_w'],
            self.data['tn_h'],
            self.data['board'],
        )

    def __getattr__(self, name):
        if name in self.interface_field_map:
            return self.data[self.interface_field_map[name]]
        raise AttributeError("'{0}' object has no attribute '{1}'".format(
            self.__class__.__name__,
            name,
        ))

    @property
    def image(self):
        return self._image

    def __repr__(self):
        return "<Post {0}/{1}>".format(self.board, self.post_no)

    def __eq__(self, other):
        return isinstance(other, Post) and self.post_no == other.post_no
