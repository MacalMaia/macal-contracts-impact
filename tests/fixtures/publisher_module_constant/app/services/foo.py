TOPIC = "qux.module-constant"
EVENT_TYPE = "qux.module_constant"


async def publish_qux(publisher, item_id: str) -> None:
    await publisher.publish(
        topic=TOPIC,
        event_type=EVENT_TYPE,
        data={"item_id": item_id},
    )


async def publish_qux_local_shadow(publisher, item_id: str) -> None:
    """A function-local name is not a module constant — stays unresolved."""
    topic = "qux.local-shadow"
    await publisher.publish(
        topic=topic,
        event_type=EVENT_TYPE,
        data={"item_id": item_id},
    )
