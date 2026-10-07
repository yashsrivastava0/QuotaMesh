"""Bounded local activity notifications. Payloads contain no request/response bodies."""

import asyncio


class ActivityFeed:
    def __init__(self):
        self.sequence = 0
        self.subscribers = set()

    def subscribe(self):
        if len(self.subscribers) >= 64:
            return None
        queue = asyncio.Queue(maxsize=64)
        self.subscribers.add(queue)
        return queue

    def publish(self, values):
        self.sequence += 1
        event = {
            "id": self.sequence,
            "request_id": values["request_id"],
            "profile": values.get("profile_slug"),
            "outcome": values["outcome"],
        }
        for queue in tuple(self.subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"id": self.sequence, "refresh": True})
            else:
                queue.put_nowait(event)
