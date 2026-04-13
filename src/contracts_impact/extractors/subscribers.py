from __future__ import annotations

import re

from contracts_impact.models import ExtractionWarning, HttpProvider, TopicSubscribed

EVENT_PATH_RE = re.compile(r"/events/(?P<topic>[^/]+)$")


def extract(
    providers: list[HttpProvider],
) -> tuple[list[TopicSubscribed], list[ExtractionWarning]]:
    """Find pub/sub subscribers by scanning HTTP providers for paths matching
    `/events/<topic>$`. The URL last segment is the canonical topic name —
    no registry, no path aliasing. Routes whose URL doesn't match this convention
    are not detected as subscribers.
    """
    subscribers: list[TopicSubscribed] = []
    warnings: list[ExtractionWarning] = []
    seen: set[tuple[str, str, int]] = set()

    for provider in providers:
        match = EVENT_PATH_RE.search(provider.path)
        if not match:
            continue
        topic = match.group("topic")
        key = (topic, provider.handler, provider.line)
        if key in seen:
            continue
        seen.add(key)
        subscribers.append(
            TopicSubscribed(
                topic=topic,
                handler=provider.handler,
                line=provider.line,
            )
        )

    return subscribers, warnings
