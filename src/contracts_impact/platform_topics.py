"""Static snapshot of the macal platform pub/sub topic registry.

Source of truth: auction-engine/scripts/init-pubsub.py. This snapshot is
embedded in the tool so that per-repo CI extraction (which only checks out
the current service) can still resolve canonical topic names without needing
auction-engine to be present.

When init-pubsub.py changes, regenerate this constant by running:
    contracts-impact dump-topics ~/macal/auction-engine/scripts/init-pubsub.py
and committing the updated file.
"""

from __future__ import annotations

# Each entry: (canonical_topic_name, target_url_var, path_after_var)
PLATFORM_TOPICS: list[tuple[str, str, str]] = [
    ("wallet.balance-changed", "AUCTION_ENGINE_URL", "/api/v1/events/wallet.balance-changed"),
    ("auction.item.adjudicated", "USERS_API_URL", "/api/v1/events/auction.item.adjudicated"),
    ("payment.capture-requested", "PAYMENT_GATEWAY_URL", "/api/v1/events/payment.capture-requested"),
    ("payment.capture-result", "USERS_API_URL", "/api/v1/events/payment.capture-result"),
    ("auction.status-changed", "USERS_API_URL", "/api/v1/events/auction.status-changed"),
    ("binding-offer.status-changed", "USERS_API_URL", "/api/v1/events/binding-offer.status-changed"),
    ("payment.status-changed", "USERS_API_URL", "/api/v1/events/payment.status-changed"),
    ("defontana.client-sync-needed", "MACAL_API_URL", "/api/v4/defontana/events/client-sync-needed"),
    ("defontana.guarantee-voucher-needed", "MACAL_API_URL", "/api/v4/defontana/events/guarantee-voucher-needed"),
    ("defontana.capture-voucher-needed", "MACAL_API_URL", "/api/v4/defontana/events/capture-voucher-needed"),
    ("defontana.invoice-needed", "MACAL_API_URL", "/api/v4/defontana/events/invoice-needed"),
    ("payout.status-changed", "USERS_API_URL", "/api/v1/events/payout.status-changed"),
]


def topic_names() -> list[str]:
    return [t[0] for t in PLATFORM_TOPICS]


def topic_by_path_suffix(suffix: str) -> str | None:
    """Return the canonical topic name whose declared push path matches the given suffix."""
    for topic, _var, path in PLATFORM_TOPICS:
        if path == suffix:
            return topic
    return None


def topics_targeting(url_var: str) -> list[tuple[str, str]]:
    """Return [(topic, push_path)] for every topic targeting the given URL var."""
    return [(topic, path) for topic, var, path in PLATFORM_TOPICS if var == url_var]
