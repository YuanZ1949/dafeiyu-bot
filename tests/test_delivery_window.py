from dafeiyu.delivery.window import ChatWindow, due_at, note, on_flushed

PARAMS = {"batch_seconds": 45, "debounce": 8, "max_wait": 30, "ambient_gap": 90}


def test_plain_messages_flush_after_45s():
    w = ChatWindow()
    note(w, now=100.0, addressed=False)
    assert due_at(w, **PARAMS) == 145.0


def test_debounce_resets_but_has_hard_cap():
    w = ChatWindow()
    note(w, now=100.0, addressed=True)
    assert due_at(w, **PARAMS) == 108.0

    note(w, now=105.0, addressed=True)
    assert due_at(w, **PARAMS) == 113.0

    # 第一条点名起 30s 是硬上限，防抖再被打断也不能超过它。
    note(w, now=125.0, addressed=True)
    assert due_at(w, **PARAMS) == 130.0


def test_ambient_gap_delays_flush():
    w = ChatWindow(last_ambient_at=100.0)
    note(w, now=120.0, addressed=False)
    # max(120+45, 100+90)
    assert due_at(w, **PARAMS) == 190.0


def test_on_flushed_clears_window_and_records_ambient_time():
    w = ChatWindow()
    note(w, now=100.0, addressed=False)
    on_flushed(w, now=150.0, addressed=False)
    assert w.first_queued_at is None
    assert w.addressed_first_at is None
    assert w.addressed_last_at is None
    assert w.last_ambient_at == 150.0
    assert due_at(w, **PARAMS) is None


def test_addressed_flush_does_not_set_ambient_time():
    w = ChatWindow(last_ambient_at=100.0)
    note(w, now=120.0, addressed=True)
    on_flushed(w, now=150.0, addressed=True)
    # 点名回复不算环境发言，不该推迟下一条非点名消息。
    assert w.last_ambient_at == 100.0
