# SPDX-License-Identifier: MIT
"""Bounded command response polling while servicing ISP control traffic."""
import time


class StartDeadline:
    """Firmware inactivity budget, bounded independently by wall time."""
    def __init__(self, clock=time.monotonic, inactivity=30, wall=40):
        self.clock = clock
        start = clock()
        self.wall_deadline = start + wall
        self.idle_deadline = start + inactivity
        self.inactivity = inactivity
        self.serviced_requests = 0

    def serviced(self, serviced):
        if serviced:
            self.serviced_requests += 1
            self.idle_deadline = min(self.clock() + self.inactivity, self.wall_deadline)

    def expired(self):
        now = self.clock()
        return now >= self.wall_deadline or now >= self.idle_deadline

    def check(self, on_timeout):
        if self.expired():
            on_timeout()
            raise TimeoutError("CH_START deadline")

    def snapshot(self):
        return {'wall_deadline': self.wall_deadline, 'idle_deadline': self.idle_deadline,
                'serviced_requests': self.serviced_requests}



def wait_command(read_response, expected, pump, guard, *, timeout,
                 start_deadline=None, on_timeout=None, clock=time.monotonic,
                 sleep=time.sleep):
    """Return only an on-time matching ACK; retain resources on failure."""
    if timeout <= 0:
        raise ValueError('Invalid ISP command timeout')
    deadline = clock() + timeout
    response = None

    def check():
        if ((start_deadline is not None and start_deadline.expired()) or
                (start_deadline is None and clock() >= deadline)):
            if on_timeout:
                on_timeout(response)
            raise TimeoutError('ISP command response')

    while True:
        guard()
        check()
        response = read_response()
        check()
        if response[0] == expected:
            return response
        serviced = pump()
        if start_deadline is not None:
            start_deadline.serviced(serviced)
        check()
        sleep(.002)
