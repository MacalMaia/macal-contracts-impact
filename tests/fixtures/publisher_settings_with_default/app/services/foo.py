from app.core.config import settings


async def publish_foo(publisher, item_id: str) -> None:
    await publisher.publish(
        topic=settings.PUBSUB_TOPIC_FOO,
        event_type="foo.canonical_name",
        data={"item_id": item_id},
    )
