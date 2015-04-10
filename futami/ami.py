# -*- coding: utf-8 -*-

from collections import defaultdict
from itertools import chain
from operator import itemgetter
from multiprocessing import (
    SimpleQueue,
    Process,
)
from time import sleep
import logging

from retrying import retry
import requests

from futami.common import (
    Action,
    BoardTarget,
    SubscriptionUpdate,
    Post,
    ThreadTarget,
)

SLEEP_TIME = 3  # seconds

THREAD_LIST = "https://a.4cdn.org/{board}/threads.json"
THREAD = "https://a.4cdn.org/{board}/res/{thread}.json"


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

def flatten(lst):
    return chain.from_iterable(lst)


class Ami:
    def __init__(self, request_queue, response_queue):
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.update_request_queue = SimpleQueue()

        Process(
            target=self.update_loop,
            args=(response_queue, self.update_request_queue),
        ).start()

        logger.debug("initialization complete")

        self.request_loop()

    # Loop to handle fast part of LoadAndFollow and other requests from IRC
    def request_loop(self):
        # The identifier argument is an opaque
        # identifier used by the queue client in some situations.
        while True:
            request = self.request_queue.get()
            logger.debug("Got request {}".format(request))

            if request.action is Action.LoadAndFollow:
                if isinstance(request.target, BoardTarget):
                    # Download all threads
                    board = request.target.board
                    threads = self.get_board(board)

                    # Seed seen_boards so update_loop doesn't re-fetch them
                    self.update_request_queue.put(SubscriptionUpdate.make(
                        action=Action.InternalQueueUpdate,
                        target=request.target,
                        payload={thread['no']: thread['last_modified'] for thread in threads},
                    ))

                    threads.sort(key=itemgetter('last_modified'))

                    # Download all thread content so we can get the OP
                    for thread in threads:
                        posts = list(self.get_thread(board, thread['no']))
                        op = posts[0]
                        op.identifier = request.identifier

                        self.response_queue.put(op)

                elif isinstance(request.target, ThreadTarget):
                    posts = list(self.get_thread(
                        request.target.board,
                        request.target.thread
                    ))

                    self.update_request_queue.put(SubscriptionUpdate.make(
                        action=Action.InternalQueueUpdate,
                        target=request.target,
                        payload=posts,
                    ))

                    for post in posts:
                        post.identifier = request.identifier

                        self.response_queue.put(post)

    @retry
    def get_board(self, board):
        url = THREAD_LIST.format(board=board)
        pages = requests.get(url).json()
        threads = list(flatten([page['threads'] for page in pages]))
        return threads

    @retry
    def get_thread(self, board, thread):
        url = THREAD.format(board=board, thread=thread)
        posts = requests.get(url).json()['posts']

        for post in posts:
            post['board'] = board

        posts = map(Post, posts)

        return posts

    # Timed loop to hit 4chan API
    def update_loop(self, response_queue, update_request_queue):
        # Set of boards that are watched
        watched_boards = set()
        # Dictionary of board => set of threads(string) that are watched
        watched_threads = defaultdict(set)

        # Dictionary of board => {thread_no => last_modified} last seen on board
        seen_boards = defaultdict(dict)
        # Dictionary of board, thread => posts last seen on thread
        seen_threads = defaultdict(lambda: defaultdict(list))

        while True:
            # Process pending update requests
            while not update_request_queue.empty():
                request = update_request_queue.get()
                if request.action is Action.InternalQueueUpdate:
                    if isinstance(request.target, BoardTarget):
                        watched_boards.add(request.target.board)
                        seen_boards[board] = request.target.payload
                    elif isinstance(request.target, ThreadTarget):
                        # assert request.target.board in watched_boards, "Asked to watch a thread of a board not currently being watched"
                        watched_threads[request.target.board].add(request.target.thread)
                        seen_threads[request.target.board][request.target.thread] = request.target.payload

            # Fetch pending boards
            pending_boards = defaultdict(dict)
            for board in watched_boards:
                pending_boards[board] = {
                    thread['no']:
                    thread['last_modified'] for thread in self.get_board(board)
                }

            to_delete = []
            for board, threads in pending_boards.items():
                for thread_no, last_modified in threads.items():
                    if thread_no not in seen_boards[board]:
                        thread = list(self.get_thread(board, thread_no))[0]
                        logger.debug("sending new thread {}".format(thread))
                        response_queue.put(thread)
                    elif last_modified > seen_boards[board][thread_no]:
                        thread = list(self.get_thread(board, thread_no))[0]
                        logger.debug("sending updated thread {}".format(thread))
                        response_queue.put(thread)
                    elif last_modified < seen_boards[board][thread_no]:
                        # Sometimes we get stale data immediately after reading
                        # it (tested under SLEEP_TIME = 3). Ignore this data.
                        to_delete.append((board, thread_no))

            for board, thread_no in to_delete:
                del pending_boards[board][thread_no]

            seen_boards = pending_boards

            # Fetch pending threads
            pending_threads = defaultdict(lambda: defaultdict(list))
            for board, threads in watched_threads.items():
                for thread in threads:
                    pending_threads[board][thread] = list(self.get_thread(board, thread))

            for board, threads in pending_threads.items():
                for thread_no, posts in threads.items():
                    for post in posts:
                        if post not in seen_threads[board][thread_no]:
                            logger.debug("sending new post {}".format(post))
                            response_queue.put(post)

            seen_threads = pending_threads

            sleep(SLEEP_TIME)
