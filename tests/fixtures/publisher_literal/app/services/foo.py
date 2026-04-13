async def publish_foo(publisher, item_id: str) -> None:
    await publisher.publish(
        topic="foo.literal-topic",
        event_type="foo.literal_topic",
        data={"item_id": item_id},
    )
