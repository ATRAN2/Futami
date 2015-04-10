# -*- coding: utf-8 -*-

from collections import namedtuple
from html.parser import HTMLParser
import enum
import re

SUMMARY_MAX_WORDS = 15

BOARD_TO_DESCRIPTION = {
    '3': '3DCG',
    'a': 'Anime & Manga',
    'an': 'Animals & Nature',
    'asp': 'Alternative Sports',
    'b': 'Random',
    'biz': 'Business & Finance',
    'c': 'Anime/Cute',
    'co': 'Comics & Cartoons',
    'cgl': 'Cosplay & EGL',
    'ck': 'Cooking',
    'cm': 'Cute/Male',
    'diy': 'Do-It-Yourself',
    'f': 'Flash',
    'fa': 'Fashion',
    'fit': 'Health & Fitness',
    'g': 'Technology',
    'gd': 'Graphic Design',
    'i': 'Oekaki',
    'ic': 'Artwork/Critique',
    'int': 'International',
    'jp': 'Otaku Culture',
    'k': 'Weapons',
    'm': 'Mecha',
    'mu': 'Music',
    'n': 'Transportation',
    'o': 'Auto',
    'out': 'Outdoors',
    'p': 'Photography',
    'po': 'Papercraft & Origami',
    'pol': 'Politically Incorrect',
    'sci': 'Science & Math',
    'sp': 'Sports',
    'tg': 'Traditional Games',
    'toy': 'Toys',
    'tv': 'Television & Film',
    'u': 'Yuri',
    'v': 'Video Games',
    'vg': 'Video Game Generals',
    'vp': 'Pokemon',
    'vr': 'Retro Games',
    'w': 'Anime/Wallpapers',
    'wg': 'Wallpapers/General',
    'wsg': 'Worksafe GIF',
}
unescape = HTMLParser().unescape

class SubscriptionUpdate(namedtuple('SubscriptionUpdate', ['action', 'target', 'identifier'])):
    @classmethod
    def make(cls, action, target, payload=None):
        """Make a Subscription with optional payload. This payload has no intrinsic meaning
        to a SubscriptionUpdate and its use may depend on the queue producer and consumer.
        """
        return cls(action, target, payload)

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
        return Image.IMAGE.format(board=self.board, tim=self.tim, ext=self.ext)

    @property
    def thumb_url(self):
        return Image.IMAGE_THUMB.format(board=self.board, tim=self.tim)

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
        'raw_comment': 'com',
        'board': 'board',
    }

    identifier = None

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

    def __repr__(self):
        return "<Post {0}/{1}>".format(self.board, self.post_no)

    def __eq__(self, other):
        return isinstance(other, Post) and self.post_no == other.post_no

    @property
    def comment(self):
        comment = self.clean(self.raw_comment)

        if self.image:
            comment = "[{}] {}".format(self.image.image_url, comment)

        return comment

    @property
    def summary(self):
        comment = self.clean(self.raw_comment)

        if not comment:
            comment = '(no post text)'
        else:
            if '\n' in comment:
                first_line, _ = comment.split('\n', 1)
            else:
                first_line = comment

            words = first_line.split(' ')
            ellipsis = '...' if len(words) > SUMMARY_MAX_WORDS else ''
            words = ' '.join(words[:SUMMARY_MAX_WORDS]) + ellipsis

        if self.image:
            comment = "[{}] {}".format(self.image.image_url, comment)

        return comment

    @property
    def image(self):
        if not self.data['tim']:
            return None
        return self._image

    @property
    def is_reply(self):
        """Posts whose backing 'resto' property is 0 are OPs.
        This is important for the response queue handler to use this property
        as a rudimentary routing mechanism.
        """
        return self.reply_to != 0

    def clean(self, text):
        if not text:
            return text

        # Some text escaping
        text = re.sub(r'\[(banned|moot)\]', r'[\1:lit]', text)

        # Code tags
        text = re.sub(r'<pre [^>]*>', r'[code]', text)
        text = re.sub(r'</pre>', r'[/code]', text)

        # Comment too long, exif tag toggle
        text = re.sub(r'<span class="abbr">.*?</span>', r'', text)

        # USER WAS * FOR THIS POST
        text = re.sub(r'<(?:b|strong) style="color:\s*red;">(.*?)</(?:b|strong)>', r'\x0304\1\x0f[/banned]', text)

        # moot text
        text = re.sub(r'<div style="padding: 5px;margin-left: \.5em;border-color: #faa;border: 2px dashed rgba\(255,0,0,\.1\);border-radius: 2px">(.*?)</div>', r'[moot]\1[/moot]', text)

        # Bold text
        text = re.sub(r'<(?:b|strong)>(.*?)</(?:b|strong)>', '\x02\1\x02', text)

        # Who are you quoting?
        text = re.sub(r'<font class="unkfunc">(.*?)</font>', '\x0303\1\x0f', text)
        text = re.sub(r'<span class="quote">(.*?)</span>', '\x0303\1\x0f', text)
        text = re.sub(r'<span class="(?:[^"]*)?deadlink">(.*?)</span>', '\x0303\1\x0f', text)

        # Get rid of links
        text = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', text)

        # Spoilers
        text = re.sub(r'<span class="spoiler"[^>]*>', '\x0301,01', text)
        text = re.sub(r'</span>', '\x0f', text)

        text = re.sub(r'<s>', '\x0301,01', text)
        text = re.sub(r'</s>', '\x0f', text)

        # <wbr>
        text = re.sub(r'<wbr>', '', text)

        # Newlines
        text = re.sub(r'<br>', ' ', text)

        text = unescape(text)

        return text
