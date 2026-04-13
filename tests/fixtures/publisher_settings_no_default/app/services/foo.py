from app.core.config import settings


async def publish_bar(publisher, item_id: str) -> None:
    await publisher.publish(
        topic=settings.PUBSUB_TOPIC_BAR,
        event_type="bar.unresolved",
        data={"item_id": item_id},
    )
