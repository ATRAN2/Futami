# -*- coding: utf-8 -*-

from collections import defaultdict
from itertools import chain
from operator import itemgetter
from multiprocessing import (
    SimpleQueue,
    Process,
)
from time import sleep

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


def flatten(lst):
    return chain.from_iterable(lst)


class Ami:
    def __init__(self, request_queue, response_queue):
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.update_request_queue = SimpleQueue()

        Process(
            target=self.update_loop,
            args=(self.response_queue, self.update_request_queue),
        ).start()

        self.request_loop()

    def request_loop(self):
        # The identifier argument is an opaque
        # identifier used by the queue client in some situations.
        while True:
            request, identifier = self.request_queue.get()

            if request.action is Action.LoadAndFollow:
                if isinstance(request.target, BoardTarget):
                    # Download all threads
                    board = request.target.board
                    threads = self.get_board(board)

                    # Seed seen_boards so the update loop doesn't re-fetch all of
                    # them
                    self.update_request_queue.put(SubscriptionUpdate(
                        Action.InternalQueueUpdate,
                        (board, {thread['no']: thread['last_modified'] for thread in threads})
                    ))

                    threads.sort(key=itemgetter('last_modified'))

                    # Download all thread content so we can get the OP
                    for thread in threads:
                        posts = list(self.get_thread(board, thread['no']))
                        op = posts[0]
                        op.identifier = identifier

                        self.response_queue.put(op)

                elif isinstance(request.target, ThreadTarget):
                    posts = list(self.get_thread(request.target.board, request.target.thread))

                    for post in posts:
                        post.identifier = identifier

                        self.response_queue.put(post)

            print("Submitting to update request queue")
            self.update_request_queue.put(request)

    def get_board(self, board):
        url = THREAD_LIST.format(board=board)
        pages = requests.get(url).json()
        threads = list(flatten([page['threads'] for page in pages]))
        return threads

    def get_thread(self, board, thread):
        url = THREAD.format(board=board, thread=thread)
        posts = requests.get(url).json()['posts']

        for post in posts:
            post['board'] = board

        posts = map(Post, posts)

        return posts

    def update_loop(self, response_queue, update_request_queue):
        # List of boards that are watched
        watched_boards = set()
        # Dictionary of board => list of threads(string) that are watched
        watched_threads = defaultdict(list)
        seen_boards = defaultdict(list)

        while True:
            # Process pending update requests
            while not update_request_queue.empty():
                request = update_request_queue.get()
                if request.action is Action.LoadAndFollow:
                    if isinstance(request.target, BoardTarget):
                        # The InternalQueueUpdate the is fired _before_ this
                        # event will have added the board for us.
                        pass
                    elif isinstance(request.target, ThreadTarget):
                        assert request.target.board in watched_boards, "Asked to watch a thread of a board not currently being watched"
                        watched_threads[request.target.board].append(request.target.thread)
                elif request.action is Action.InternalQueueUpdate:
                    board, seen = request.target
                    watched_boards.add(board)
                    seen_boards[board] = seen

            # Fetch pending boards
            pending_boards = defaultdict(dict)
            for board in watched_boards:
                pending_boards[board] = {thread['no']: thread['last_modified'] for thread in self.get_board(board)}

            to_delete = []
            for board, threads in pending_boards.items():
                for thread_no, last_modified in threads.items():
                    if thread_no not in seen_boards[board]:
                        thread = list(self.get_thread(board, thread_no))[0]
                        response_queue.put(thread)
                    elif last_modified > seen_boards[board][thread_no]:
                        thread = list(self.get_thread(board, thread_no))[0]
                        response_queue.put(thread)
                    elif last_modified < seen_boards[board][thread_no]:
                        # Sometimes we get stale data immediately after reading
                        # it (tested under SLEEP_TIME = 3). Ignore this data.
                        to_delete.append((board, thread_no))

            for board, thread_no in to_delete:
                del pending_boards[board][thread_no]

            # Fetch pending threads
            # TODO: Not implemented yet

            seen_boards = pending_boards

            sleep(SLEEP_TIME)

if __name__ == "__main__":

    request = SubscriptionUpdate(Action.LoadAndFollow, BoardTarget('a'))
    # request2 = SubscriptionUpdate(Action.LoadAndFollow, ThreadTarget('jp', 13265004))
    response_queue = Queue()

    ami = Ami(response_queue)
    ami.request(request)
    # ami.request(request2)
