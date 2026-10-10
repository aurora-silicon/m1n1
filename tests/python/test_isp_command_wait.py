import pytest

from m1n1.fw.isp.isp_command import StartDeadline, wait_command


def test_matching_ack_returns_without_pump():
    calls = []
    response = (0x101, 12, 0, 0, 0, 0, 0, 0)
    assert wait_command(lambda: response, 0x101, lambda: calls.append('pump'),
        lambda: calls.append('guard'), timeout=5, clock=lambda: 0) == response
    assert calls == ['guard']


@pytest.mark.parametrize('startup', [False, True])
def test_blocking_read_late_ack_is_rejected(startup):
    now = [0]
    calls = []
    response = (0x101, 12, 0, 0, 0, 0, 0, 0)
    def read():
        now[0] = 41
        return response
    deadline = StartDeadline(lambda: now[0]) if startup else None
    with pytest.raises(TimeoutError):
        wait_command(read, 0x101, lambda: calls.append('pump'), lambda: None,
            timeout=5, start_deadline=deadline, clock=lambda: now[0],
            on_timeout=lambda rsp: calls.append(rsp))
    assert calls == [response]


def test_expired_guard_prevents_read_and_pump():
    now = [0]
    calls = []
    def guard():
        now[0] = 6
    with pytest.raises(TimeoutError):
        wait_command(lambda: calls.append('read'), 1, lambda: calls.append('pump'),
            guard, timeout=5, clock=lambda: now[0])
    assert not calls


def test_serviced_requests_extend_idle_but_not_wall():
    now = [0]
    calls = []
    deadline = StartDeadline(lambda: now[0])
    def pump():
        calls.append('pump')
        now[0] += 20
        return True
    with pytest.raises(TimeoutError):
        wait_command(lambda: (0,) * 8, 1, pump, lambda: None, timeout=30,
            start_deadline=deadline, clock=lambda: now[0], sleep=lambda _: None)
    assert len(calls) == 2
    assert deadline.wall_deadline == 40 and deadline.serviced_requests == 2


def test_unserviced_traffic_does_not_extend_idle():
    now = [0]
    deadline = StartDeadline(lambda: now[0])
    def pump():
        now[0] += 16
        return False
    with pytest.raises(TimeoutError):
        wait_command(lambda: (0,) * 8, 1, pump, lambda: None, timeout=30,
            start_deadline=deadline, clock=lambda: now[0], sleep=lambda _: None)
    assert now[0] == 32 and deadline.serviced_requests == 0


def test_pump_exception_preserved_without_further_access():
    calls = []
    def pump():
        raise RuntimeError('DART fault')
    with pytest.raises(RuntimeError, match='DART fault'):
        wait_command(lambda: calls.append('read') or (0,) * 8, 1, pump,
            lambda: None, timeout=5, clock=lambda: 0)
    assert calls == ['read']
