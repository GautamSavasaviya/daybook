import logging
import threading

import pytest

from daybook.services import safe
from daybook.services.events import Changed, Entity, EventBus, Toast


class Queue:
    """Stand-in for GLib.idle_add: collects callbacks to run later on the 'main loop'."""

    def __init__(self) -> None:
        self.callbacks = []

    def __call__(self, callback) -> None:
        self.callbacks.append(callback)

    def run(self) -> None:
        while self.callbacks:
            self.callbacks.pop(0)()


@pytest.fixture
def reported(monkeypatch):
    errors: list[BaseException] = []
    safe.set_error_reporter(errors.append)
    return errors


def test_publish_on_main_thread_dispatches_immediately_by_type():
    bus = EventBus(Queue())
    changed, toasts = [], []
    bus.subscribe(Changed, changed.append)
    bus.subscribe(Toast, toasts.append)
    bus.publish(Changed(Entity.TASK, frozenset({3})))
    assert changed == [Changed(Entity.TASK, frozenset({3}))]
    assert toasts == []


def test_unsubscribe_stops_delivery_and_is_idempotent():
    bus = EventBus(Queue())
    seen = []
    sub = bus.subscribe(Changed, seen.append)
    sub.unsubscribe()
    sub.unsubscribe()
    bus.publish(Changed(Entity.SETTINGS))
    assert seen == []


def test_publish_from_another_thread_is_marshalled_to_the_main_loop():
    queue = Queue()
    bus = EventBus(queue)
    seen = []
    bus.subscribe(Changed, lambda e: seen.append((e, threading.current_thread())))
    worker = threading.Thread(target=lambda: bus.publish(Changed(Entity.PLAN)))
    worker.start()
    worker.join()
    assert seen == []
    queue.run()
    assert seen == [(Changed(Entity.PLAN), threading.main_thread())]


def test_failing_handler_is_reported_and_others_still_run(reported):
    bus = EventBus(Queue())
    seen = []

    def broken(_event):
        raise RuntimeError("handler bug")

    bus.subscribe(Changed, broken)
    bus.subscribe(Changed, seen.append)
    bus.publish(Changed(Entity.TASK))
    assert len(seen) == 1
    assert [str(e) for e in reported] == ["handler bug"]


def test_failing_toast_handler_is_only_logged(reported, caplog):
    bus = EventBus(Queue())
    bus.subscribe(Toast, lambda _e: 1 / 0)
    with caplog.at_level(logging.ERROR):
        bus.publish(Toast("hello"))
    assert reported == []
    assert "toast handler failed" in caplog.text


def test_safe_callback_passes_results_through():
    assert safe.safe_callback(lambda a, b=2: a + b)(1, b=5) == 6


def test_safe_callback_reports_and_returns_none(reported, caplog):
    @safe.safe_callback
    def explode() -> bool:
        raise ValueError("bad")

    with caplog.at_level(logging.ERROR):
        assert explode() is None
    assert [type(e) for e in reported] == [ValueError]
    assert "unexpected error" in caplog.text


def test_report_error_survives_a_broken_reporter(caplog):
    def broken_reporter(_exc):
        raise OSError("no display")

    safe.set_error_reporter(broken_reporter)
    with caplog.at_level(logging.ERROR):
        safe.report_error(KeyError("x"))
    assert "error reporter itself failed" in caplog.text


def test_report_error_without_reporter_only_logs(caplog):
    with caplog.at_level(logging.ERROR):
        safe.report_error(KeyError("x"))
    assert "unexpected error" in caplog.text


def test_error_message_text():
    assert safe.error_message() == "Something went wrong — details in log"


def test_failing_toast_display_cannot_loop_through_the_error_reporter(caplog):
    """The app's reporter shows errors as a Toast; a broken Toast handler must not recurse."""
    bus = EventBus(Queue())
    toasts_attempted = []

    def broken_toast_handler(event):
        toasts_attempted.append(event)
        raise RuntimeError("overlay is gone")

    def broken_changed_handler(_event):
        raise ValueError("view bug")

    safe.set_error_reporter(lambda _exc: bus.publish(Toast(safe.error_message())))
    bus.subscribe(Toast, broken_toast_handler)
    bus.subscribe(Changed, broken_changed_handler)
    with caplog.at_level(logging.ERROR):
        bus.publish(Changed(Entity.TASK))
    assert len(toasts_attempted) == 1
    assert "toast handler failed" in caplog.text
