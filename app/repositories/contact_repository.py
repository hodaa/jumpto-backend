"""Repository for contact messages."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import ContactMessage

logger = get_logger(__name__)


class ContactRepository:
    """Repository for ContactMessage operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, name: str, email: str, message: str) -> ContactMessage:
        """Create a new contact message record."""
        contact_message = ContactMessage(
            name=name,
            email=email,
            message=message,
        )
        self.session.add(contact_message)
        await self.session.flush()
        logger.info("Stored contact message", contact_id=contact_message.id)
        return contact_message
