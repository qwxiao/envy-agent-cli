"""重试策略测试：有边界重试的四要素 + 流式重试的不可回放约束。"""

from envy_agent_cli.llm.events import Error, LLMErrorCode, TextDelta
from envy_agent_cli.llm.retry import RetryPolicy


def test_should_retry_only_when_adapter_says_so():
    """是否可重试由 Adapter 决定，策略层不推导。"""
    policy = RetryPolicy()
    assert policy.should_retry(Error.from_code(LLMErrorCode.RATE_LIMITED, "429"), attempt=1) is True
    assert policy.should_retry(Error.from_code(LLMErrorCode.AUTH_FAILED, "401"), attempt=1) is False


def test_should_retry_respects_max_attempts():
    policy = RetryPolicy(max_attempts=3)
    err = Error.from_code(LLMErrorCode.TIMEOUT, "timeout")
    assert policy.should_retry(err, attempt=2) is True
    assert policy.should_retry(err, attempt=3) is False   # 预算耗尽 → 交 Loop 降级


def test_backoff_grows_exponentially_with_jitter_and_cap():
    policy = RetryPolicy(base_delay=1.0, backoff_factor=2.0, max_delay=5.0, jitter_ratio=0.25)
    # 抖动是 ±25%，所以取多次采样的区间来判断
    for attempt, expected in ((1, 1.0), (2, 2.0), (3, 4.0)):
        samples = [policy.delay_for(attempt) for _ in range(50)]
        assert all(abs(s - expected) <= expected * 0.25 + 1e-9 for s in samples)
    # 封顶：指数增长不能无限涨
    assert all(s <= 5.0 * 1.25 for s in (policy.delay_for(10) for _ in range(50)))


def test_retry_stream_retries_when_nothing_emitted_yet():
    """失败发生在吐出任何事件之前 → 可以安全重试（等价于请求没生效）。"""
    calls, slept = [], []

    def open_stream():
        calls.append(1)
        if len(calls) == 1:
            yield Error.from_code(LLMErrorCode.SERVER_ERROR, "boom")
        else:
            yield TextDelta("ok")

    events = list(RetryPolicy().retry_stream(open_stream, sleep=slept.append))
    assert events == [TextDelta("ok")]
    assert len(calls) == 2 and len(slept) == 1


def test_retry_stream_does_not_replay_after_partial_output():
    """已经吐出正文再失败 → 不能重放，否则上层会收到重复内容。"""
    calls = []

    def open_stream():
        calls.append(1)
        yield TextDelta("前半段")
        yield Error.from_code(LLMErrorCode.SERVER_ERROR, "断了")

    events = list(RetryPolicy().retry_stream(open_stream, sleep=lambda _: None))
    assert events[0] == TextDelta("前半段")
    assert isinstance(events[-1], Error)
    assert len(calls) == 1, "部分输出之后不允许重试"


def test_retry_stream_gives_up_on_non_retryable_error():
    calls = []

    def open_stream():
        calls.append(1)
        yield Error.from_code(LLMErrorCode.AUTH_FAILED, "401")

    events = list(RetryPolicy().retry_stream(open_stream, sleep=lambda _: None))
    assert len(calls) == 1 and isinstance(events[0], Error)


def test_retry_stream_stops_after_budget_exhausted():
    calls = []

    def open_stream():
        calls.append(1)
        yield Error.from_code(LLMErrorCode.TIMEOUT, "timeout")

    policy = RetryPolicy(max_attempts=3)
    events = list(policy.retry_stream(open_stream, sleep=lambda _: None))
    assert len(calls) == 3
    assert isinstance(events[-1], Error) and events[-1].retryable is True
