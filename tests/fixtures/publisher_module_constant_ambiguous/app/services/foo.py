TOPIC = "quux.first-binding"
TOPIC = "quux.second-binding"


async def publish_quux(publisher, item_id: str) -> None:
    await publisher.publish(
        topic=TOPIC,
        event_type="quux.ambiguous",
        data={"item_id": item_id},
    )
