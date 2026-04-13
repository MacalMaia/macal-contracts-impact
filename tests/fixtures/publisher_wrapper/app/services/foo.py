async def _publish_event(topic: str, event_type: str, data: dict) -> None:
    publisher = _get_publisher()
    await publisher.publish(topic=topic, event_type=event_type, data=data)


def _get_publisher():
    return None


async def trigger_baz() -> None:
    await _publish_event(
        topic="baz.wrapper-topic",
        event_type="baz.wrapper_topic",
        data={"k": "v"},
    )
