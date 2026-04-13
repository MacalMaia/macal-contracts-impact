from __future__ import annotations

import re
from pathlib import Path

from contracts_impact.models import ExtractionWarning, HttpProvider, TopicSubscribed
from contracts_impact.platform_topics import (
    PLATFORM_TOPICS,
    topic_by_path_suffix,
    topics_targeting,
)

# Match `/events/<topic>` anywhere in the path
EVENT_PATH_RE = re.compile(r"/events/(?P<topic>[^/]+)$")

SERVICE_TO_URL_VAR: dict[str, str] = {
    "auction-engine": "AUCTION_ENGINE_URL",
    "macal-users-api": "USERS_API_URL",
    "macal-api": "MACAL_API_URL",
    "payment-gateway": "PAYMENT_GATEWAY_URL",
}


def extract(
    repo_root: Path,
    service_name: str,
    providers: list[HttpProvider],
) -> tuple[list[TopicSubscribed], list[ExtractionWarning]]:
    """Find pub/sub subscribers via:
    1. Scanning HTTP providers for paths matching `/events/<topic>`
    2. Resolving each to a canonical topic name via the bundled platform topic registry
    3. Detecting orphans (topics this service should subscribe to per the registry but has no handler)
    """
    subscribers: list[TopicSubscribed] = []
    warnings: list[ExtractionWarning] = []

    for provider in providers:
        match = EVENT_PATH_RE.search(provider.path)
        if not match:
            continue
        topic = topic_by_path_suffix(provider.path) or match.group("topic")
        subscribers.append(
            TopicSubscribed(
                topic=topic,
                handler=provider.handler,
                line=provider.line,
            )
        )

    # Orphan detection: any topic in the platform registry that targets this service
    # but is not in the handler list above is an orphan.
    self_url_var = SERVICE_TO_URL_VAR.get(service_name)
    if self_url_var:
        handled_topics = {s.topic for s in subscribers}
        for topic, push_path in topics_targeting(self_url_var):
            if topic in handled_topics:
                continue
            subscribers.append(
                TopicSubscribed(topic=topic, handler=None, line=None)
            )
            warnings.append(
                ExtractionWarning(
                    kind="orphan_subscription",
                    file="<platform-topics>",
                    line=0,
                    message=(
                        f"topic {topic!r} declared in platform registry with push to "
                        f"this service ({push_path}), but no handler found in app/api/"
                    ),
                )
            )

    return subscribers, warnings
