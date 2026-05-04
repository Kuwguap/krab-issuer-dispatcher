from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Transaction:
    """
    Represents a single document transmission event.

    Mirrors the roadmap schema:
    - ID
    - Telegram Name
    - Handle
    - Filename
    - Client Details
    - Timestamp
    - Delivery Status
    """

    id: str
    telegram_name: str
    telegram_handle: Optional[str]
    filename: str
    client_details: str
    recipient_name: Optional[str]
    recipient_email: Optional[str]
    issuer_group: Optional[str]
    reference_id: Optional[str]
    timestamp: datetime
    delivery_status: str

    @classmethod
    def new(
        cls,
        id: str,
        telegram_name: str,
        telegram_handle: Optional[str],
        filename: str,
        client_details: str,
        recipient_name: Optional[str] = None,
        recipient_email: Optional[str] = None,
        issuer_group: Optional[str] = None,
        reference_id: Optional[str] = None,
        delivery_status: str = "PENDING",
    ) -> "Transaction":
        return cls(
            id=id,
            telegram_name=telegram_name,
            telegram_handle=telegram_handle,
            filename=filename,
            client_details=client_details,
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            issuer_group=issuer_group,
            reference_id=reference_id,
            timestamp=datetime.now(timezone.utc),
            delivery_status=delivery_status,
        )










