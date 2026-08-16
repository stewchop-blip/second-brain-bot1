"""
Analytics service — single interface for tracking events.
Today writes to the existing DB; later can be pointed at PostHog/Anystat
without changing handlers.
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from db import track_event

logger = logging.getLogger(__name__)


class AnalyticsService:
    @staticmethod
    async def track(
        event_name: str,
        user_id: Optional[int] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Track an analytics event. Non-blocking semantics: log + store."""
        props = properties or {}
        # Privacy: never store raw message text here
        props.setdefault("timestamp", datetime.utcnow().isoformat())
        try:
            await track_event(user_id or 0, event_name, props)
        except Exception as e:
            logger.warning(f"analytics track failed for {event_name}: {e}")


analytics = AnalyticsService()
