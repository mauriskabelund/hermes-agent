"""Regression coverage for opt-in progressive Matrix streaming."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _consumer_config(*, progressive: bool):
    runner = GatewayRunner.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.MATRIX,
        chat_id="!room:matrix.example.org",
        chat_type="group",
    )
    streaming = StreamingConfig.from_dict(
        {
            "enabled": True,
            "transport": "edit",
            "matrix_progressive": progressive,
        }
    )
    adapter = SimpleNamespace(SUPPORTS_MESSAGE_EDITING=True)
    config, _pause = runner._build_stream_consumer_config(
        source,
        streaming,
        adapter,
        on_missing_cursor="fallback",
    )
    return streaming, config


def test_matrix_progressive_is_safe_and_off_by_default():
    config = StreamingConfig.from_dict({"enabled": True})

    assert config.matrix_progressive is False
    assert config.to_dict()["matrix_progressive"] is False


def test_matrix_default_remains_buffer_only_and_cursorless():
    _streaming, consumer = _consumer_config(progressive=False)

    assert consumer.cursor == ""
    assert consumer.buffer_only is True


def test_matrix_progressive_enables_recurring_cursorless_edits():
    streaming, consumer = _consumer_config(progressive=True)

    assert streaming.matrix_progressive is True
    assert consumer.cursor == ""
    assert consumer.buffer_only is False


@pytest.mark.asyncio
async def test_character_threshold_opens_preview_but_does_not_flood_edits():
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="$preview")
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="$preview")
    )
    consumer = GatewayStreamConsumer(
        adapter,
        "!room:matrix.example.org",
        StreamConsumerConfig(
            edit_interval=0.10,
            buffer_threshold=24,
            cursor="",
            transport="edit",
        ),
    )
    task = asyncio.create_task(consumer.run())
    for _ in range(50):
        consumer.on_delta("abcdefghijkl")
        await asyncio.sleep(0.005)
    consumer.finish()
    await asyncio.wait_for(task, timeout=3)

    event_count = adapter.send.await_count + adapter.edit_message.await_count
    assert adapter.send.await_count == 1
    assert 2 <= event_count <= 6
