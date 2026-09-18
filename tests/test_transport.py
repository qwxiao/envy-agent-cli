"""传输层测试：SSE 分帧的三条硬约束。

全部离线——用"伪造的分片迭代器"模拟网络到达节奏，不需要真服务端。
这类 bug 不联网测不出来：网络把一个事件切成几片，是按行处理的经典死法。
"""

import pytest

from envy_agent_cli.llm.transport import SSE_DONE, extract_data, iter_sse_data, iter_sse_json


def chunks_of(text: str, size: int) -> list[str]:
    """把一段文本切成固定大小的分片——模拟网络到达粒度。"""
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_single_event_in_one_chunk():
    assert list(iter_sse_data(iter(['data: {"a": 1}\n\n']))) == ['{"a": 1}']


def test_event_split_across_chunks_is_reassembled():
    """核心用例：一个事件被网络拆成多片到达，必须能拼回完整 JSON 再解析。

    按行处理的话会切出半行 JSON，json.loads 直接崩。
    """
    raw = 'data: {"choices": [{"delta": {"content": "你好"}}]}\n\n'
    for size in (1, 3, 7, 11):
        assert list(iter_sse_data(iter(chunks_of(raw, size)))) == [
            '{"choices": [{"delta": {"content": "你好"}}]}'
        ]


def test_multiple_events_in_one_chunk():
    raw = 'data: {"i": 1}\n\ndata: {"i": 2}\n\n'
    assert list(iter_sse_data(iter([raw]))) == ['{"i": 1}', '{"i": 2}']


def test_trailing_buffer_without_final_blank_line():
    """流末最后一块可能没有结尾空行——不处理就会丢掉最后一个事件。"""
    raw = 'data: {"i": 1}\n\ndata: {"i": 2}'
    assert list(iter_sse_data(iter([raw]))) == ['{"i": 1}', '{"i": 2}']


def test_blocks_without_data_line_are_ignored():
    """心跳/注释行（`: keepalive`）没有 data 前缀，应被忽略而不是当正文。"""
    raw = ': keepalive\n\ndata: {"i": 1}\n\n'
    assert list(iter_sse_data(iter([raw]))) == ['{"i": 1}']


def test_multiline_data_is_joined():
    raw = 'data: {"a":\ndata: 1}\n\n'
    assert list(iter_sse_data(iter([raw]))) == ['{"a":\n1}']


def test_extract_data_returns_none_without_data_lines():
    assert extract_data(": ping") is None


def test_done_sentinel_stops_iteration_before_json_parse():
    """`data: [DONE]` 不是合法 JSON——必须在 json.loads 之前判断并终止。"""
    raw = f'data: {{"i": 1}}\n\ndata: {SSE_DONE}\n\n'
    assert list(iter_sse_json(iter([raw]))) == [{"i": 1}]


def test_malformed_json_chunk_is_skipped_not_fatal():
    """偶发的坏块不该让整条流崩掉。"""
    raw = 'data: {oops\n\ndata: {"i": 2}\n\n'
    assert list(iter_sse_json(iter([raw]))) == [{"i": 2}]


@pytest.mark.parametrize("size", [1, 2, 5, 17])
def test_full_stream_reassembles_regardless_of_chunking(size):
    raw = ('data: {"i": 1}\n\ndata: {"i": 2}\n\n'
           f'data: {{"i": 3}}\n\ndata: {SSE_DONE}\n\n')
    assert list(iter_sse_json(iter(chunks_of(raw, size)))) == [{"i": 1}, {"i": 2}, {"i": 3}]
