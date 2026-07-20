from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.invocation_scrubber import (
    StreamingInvocationScrubber,
    scrub_invocation_markup,
)


_EXACT_LEAK = (
    'call\n<invoke name="mcp__claude-proxy__terminal">\n'
    '<parameter name="command">scp -q /tmp/ct104_env_merge.sh '
    "root@10.10.0.45:/root/env_merge.sh && ssh root@10.10.0.45 "
    "'bash /root/env_merge.sh'</parameter>\n"
    '<parameter name="timeout">30</parameter>\n</invoke>'
)
_PRIVATE_SENTINELS = (
    "<invoke",
    "<parameter",
    "scp -q",
    "10.10.0.45",
    "root@",
    "/root/env_merge.sh",
)


def _adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="message-1")
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="message-1")
    )
    return adapter


def _delivered_texts(adapter: MagicMock) -> tuple[str, ...]:
    calls = (*adapter.send.call_args_list, *adapter.edit_message.call_args_list)
    return tuple(str(call.kwargs.get("content", "")) for call in calls)


async def _run_chunks(chunks: Iterable[str]) -> tuple[GatewayStreamConsumer, MagicMock]:
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(buffer_only=True, cursor=""),
    )
    for chunk in chunks:
        consumer.on_delta(chunk)
    consumer.finish()
    await consumer.run()
    return consumer, adapter


def _assert_no_private_invocation(texts: Iterable[str]) -> None:
    assert not any(
        sentinel.casefold() in text.casefold()
        for text in texts
        for sentinel in _PRIVATE_SENTINELS
    )


@pytest.mark.asyncio
async def test_exact_invocation_never_reaches_delivery_character_by_character() -> None:
    # Given: the observed invocation leak split into single-character deltas.
    # When: the gateway consumes and finalizes the stream.
    _consumer, adapter = await _run_chunks(tuple(_EXACT_LEAK))

    # Then: only public prose reaches adapter send/edit calls.
    delivered = _delivered_texts(adapter)
    _assert_no_private_invocation(delivered)
    assert not any("call" in text.casefold() for text in delivered)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        "Before\n<invoke name=\"terminal\"><parameter name=\"command\">private-command</parameter></invoke>\nAfter",
        "Before\n<invoke name=\"terminal\"><parameter name=\"command\">private-command",
        "Before\n&lt;invoke name=&quot;terminal&quot;&gt;&lt;parameter name=&quot;command&quot;&gt;private-command&lt;/parameter&gt;&lt;/invoke&gt;\nAfter",
        "Before\n&lt;invoke name=&quot;terminal&quot;&gt;&lt;parameter name=&quot;command&quot;&gt;private-command",
    ),
)
async def test_complete_unclosed_and_escaped_invocations_are_discarded(
    payload: str,
) -> None:
    # Given: complete or unclosed raw/escaped invocation markup.
    # When: adversarial boundaries split every opener and closer.
    _consumer, adapter = await _run_chunks(tuple(payload))

    # Then: invocation structure and body never reach the platform.
    delivered = _delivered_texts(adapter)
    assert not any("private-command" in text for text in delivered)
    assert not any("invoke" in text.casefold() for text in delivered)
    assert any("Before" in text for text in delivered)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    (
        '<tool_call>{"command":"private-command"}</tool_call>',
        '<tool_calls>{"command":"private-command"}</tool_calls>',
        '<tool_result>{"result":"private-command"}</tool_result>',
        '<function_call>{"command":"private-command"}</function_call>',
        '<function_calls>{"command":"private-command"}</function_calls>',
        '<function name="terminal"><parameter name="command">private-command</parameter></function>',
        '&lt;tool_call&gt;private-command&lt;/tool_call&gt;',
        '&lt;tool_calls&gt;private-command&lt;/tool_calls&gt;',
        '&lt;tool_result&gt;private-command&lt;/tool_result&gt;',
        '&lt;function_call&gt;private-command&lt;/function_call&gt;',
        '&lt;function_calls&gt;private-command&lt;/function_calls&gt;',
        '&lt;function name=&quot;terminal&quot;&gt;&lt;parameter name=&quot;command&quot;&gt;private-command&lt;/parameter&gt;&lt;/function&gt;',
    ),
)
@pytest.mark.parametrize("closed", (True, False))
async def test_documented_tool_xml_dialects_never_reach_delivery(
    block: str,
    closed: bool,
) -> None:
    # Given: a documented raw or escaped tool XML dialect.
    payload = f"Before\n{block if closed else block.split('</', 1)[0].split('&lt;/', 1)[0]}"

    # When: every character arrives as an independent stream delta.
    _consumer, adapter = await _run_chunks(tuple(payload))

    # Then: neither the block nor its private body reaches delivery.
    delivered = _delivered_texts(adapter)
    assert not any("private-command" in text for text in delivered)
    assert any("Before" in text for text in delivered)


@pytest.mark.asyncio
async def test_partial_invocation_opener_never_flashes_before_disambiguation() -> None:
    # Given: visible prose followed by an invocation opener prefix.
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
    )
    task = asyncio.create_task(consumer.run())

    # When: the first delivery tick ends at the ambiguous `<inv` prefix.
    consumer.on_delta("call\n<inv")
    for _ in range(3):
        await asyncio.sleep(0)

    # Then: neither the preamble nor the partial opener has flashed.
    assert adapter.send.await_count == 0
    assert adapter.edit_message.await_count == 0

    consumer.on_delta("oke name=\"terminal\"><parameter>private-command</parameter></invoke>")
    consumer.finish()
    await task
    assert not any("private-command" in text for text in _delivered_texts(adapter))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail",
    ("<inv", '<invoke name="terminal"', "&lt;tool_ca", "&lt;function name=&quot;terminal&quot;"),
)
async def test_stream_end_discards_partial_or_unclosed_invocation_opener(
    tail: str,
) -> None:
    # Given: a stream ends while a private invocation opener is incomplete.
    # When: the consumer finalizes before the tag can be disambiguated.
    _consumer, adapter = await _run_chunks(tuple(f"Before\n{tail}"))

    # Then: the safe prefix remains and no partial XML reaches the platform.
    delivered = _delivered_texts(adapter)
    assert any("Before" in text for text in delivered)
    assert not any(tail.casefold() in text.casefold() for text in delivered)


@pytest.mark.asyncio
async def test_ordinary_xml_and_angle_bracket_prose_remain_visible() -> None:
    # Given: ordinary XML and prose that only resembles invocation syntax.
    visible = (
        "Use <invoice>42</invoice>, <function>named</function>, "
        "<parameter>public</parameter>, and 2 < 3. "
        "The literal &lt;invoke-like&gt; text is documentation."
    )

    # When: the text streams character by character.
    _consumer, adapter = await _run_chunks(tuple(visible))

    # Then: non-invocation angle-bracket content is preserved exactly.
    assert _delivered_texts(adapter)[-1] == visible


@pytest.mark.asyncio
async def test_inline_named_function_documentation_remains_visible() -> None:
    # Given: prose documents a named function tag inline rather than invoking it.
    visible = 'You can write <function name="x">y</function> inline.'

    # When: the documentation streams one character at a time.
    _consumer, adapter = await _run_chunks(tuple(visible))

    # Then: the ordinary documentation survives unchanged.
    assert _delivered_texts(adapter)[-1] == visible


@pytest.mark.asyncio
async def test_completed_commentary_invocation_never_reaches_delivery() -> None:
    # Given: a completed interim commentary contains the observed private invocation.
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(buffer_only=True, cursor=""),
    )

    # When: commentary and stream finalization are processed.
    consumer.on_commentary(_EXACT_LEAK)
    consumer.finish()
    await consumer.run()

    # Then: no commentary content or invocation payload reaches the platform.
    assert _delivered_texts(adapter) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        "Before\n<invoke name=\"terminal\"><parameter>escaped &lt;/invoke&gt; private-tail</parameter></invoke>\nAfter",
        "Before\n<invoke name=\"terminal\"><parameter>literal </invoke> private-tail</parameter></invoke>\nAfter",
        "Before\n&lt;invoke name=\"terminal\"&gt;&lt;parameter&gt;raw </invoke> private-tail&lt;/parameter&gt;&lt;/invoke&gt;\nAfter",
        "Before\n<invoke name=\"outer\"><invoke name=\"inner\">inner</invoke>private-tail</invoke>\nAfter",
        "Before\n&#60;invoke name=\"terminal\"&#62;private-tail&#60;/invoke&#62;\nAfter",
        "Before\n&#00060;invoke name=\"terminal\"&#00062;private-tail&#00060;/invoke&#00062;\nAfter",
        "Before\n&#x3c;tool:invoke name=\"terminal\"&#x3e;private-tail&#x3c;/tool:invoke&#x3e;\nAfter",
        "Before\n&#x003c;invoke name=\"terminal\"&#x003e;private-tail&#x003c;/invoke&#x003e;\nAfter",
        "Before\n&amp;lt;invoke name=\"terminal\"&amp;gt;private-tail&amp;lt;/invoke&amp;gt;\nAfter",
    ),
)
async def test_nested_mixed_and_encoded_blocks_remain_private(payload: str) -> None:
    _consumer, adapter = await _run_chunks(tuple(payload))

    delivered = "".join(_delivered_texts(adapter))
    assert "private-tail" not in delivered
    assert "Before" in delivered
    assert "After" in delivered


@pytest.mark.asyncio
async def test_self_closing_invocation_preserves_following_public_text() -> None:
    _consumer, adapter = await _run_chunks(
        tuple('Before\n<invoke name="terminal"/>\nAfter')
    )

    assert _delivered_texts(adapter)[-1] == "Before\n\nAfter"


@pytest.mark.asyncio
async def test_unclosed_invocation_stays_private_across_segment_boundary() -> None:
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(buffer_only=True, cursor=""),
    )
    consumer.on_delta('Before\n<invoke name="terminal">private-before')
    consumer.on_segment_break()
    consumer.on_delta("private-after</invoke>\nAfter")
    consumer.finish()
    await consumer.run()

    delivered = "".join(_delivered_texts(adapter))
    assert "private-before" not in delivered
    assert "private-after" not in delivered
    assert "After" in delivered


@pytest.mark.asyncio
async def test_segment_boundary_reclassifies_function_invocation_from_fresh_context() -> None:
    # Given a public segment whose trailing prose would make an adjacent
    # <function> tag look like ordinary inline markup.
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(buffer_only=True, cursor=""),
    )
    consumer.on_delta("Public sentence without terminator")
    consumer.on_segment_break()
    consumer.on_delta('<function name="secret">PRIVATE</function>Safe after')
    consumer.finish()

    # When the queued stream is delivered.
    await consumer.run()

    # Then the new segment is classified independently and private markup
    # never reaches the adapter.
    delivered = "".join(_delivered_texts(adapter))
    assert "Public sentence without terminator" in delivered
    assert "PRIVATE" not in delivered
    assert "<function" not in delivered
    assert "Safe after" in delivered


def test_malformed_opening_tag_is_bounded_and_fails_closed() -> None:
    payload = '<invoke name="terminal" ' + ("x" * 10_000) + "private-tail"

    assert scrub_invocation_markup(payload) == ""


def test_oversized_partial_numeric_entity_is_bounded_and_fails_closed() -> None:
    scrubber = StreamingInvocationScrubber()
    payload = "&#" + ("0" * 20_000) + "private-tail"

    assert "".join(scrubber.feed(char) for char in payload) == ""
    assert scrubber.finish() == ""


@pytest.mark.parametrize(
    "opening, partial",
    (("&#60;invoke&#62;", "&#"), ("&#x3c;invoke&#x3e;", "&#x")),
)
def test_oversized_numeric_prefix_inside_private_block_is_bounded(
    opening: str,
    partial: str,
) -> None:
    scrubber = StreamingInvocationScrubber()
    assert scrubber.feed(opening) == ""

    assert "".join(scrubber.feed(char) for char in partial + ("0" * 20_000)) == ""
    assert scrubber.finish() == ""


@pytest.mark.parametrize(
    "left, right",
    (
        ("&#" + ("0" * 4090) + "60;", "&#" + ("0" * 4090) + "62;"),
        ("&#x" + ("0" * 4088) + "3c;", "&#x" + ("0" * 4088) + "3e;"),
    ),
)
def test_near_cap_numeric_delimiters_cannot_reopen_streaming_output(
    left: str,
    right: str,
) -> None:
    scrubber = StreamingInvocationScrubber()
    payload = f"Before{left}invoke{right}private-tail{left}/invoke{right}After"

    visible = "".join(scrubber.feed(char) for char in payload) + scrubber.finish()

    assert "private-tail" not in visible
    assert "invoke" not in visible


def test_reset_starts_a_fresh_public_stream_after_terminal_cleanup() -> None:
    scrubber = StreamingInvocationScrubber()
    assert scrubber.feed('<invoke name="terminal">private') == ""
    assert scrubber.finish() == ""
    scrubber.reset()

    assert scrubber.feed("public") + scrubber.finish() == "public"


@pytest.mark.parametrize(
    "text",
    ("R&D &", "Document literal &lt", "Rock &amp;", "Value &#", "Value &#000"),
)
def test_terminal_entity_prefixes_remain_public(text: str) -> None:
    assert scrub_invocation_markup(text) == text
