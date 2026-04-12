from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.ticket import Ticket, TicketMessage


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, ticket_id: UUID) -> Ticket | None:
        return await self.session.get(Ticket, ticket_id)

    async def get_open_ticket_for_user(self, user_id: UUID) -> Ticket | None:
        result = await self.session.execute(
            select(Ticket)
            .options(selectinload(Ticket.messages))
            .where(Ticket.user_id == user_id, Ticket.status.in_(["open", "answered"]))
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_ticket(self, *, user_id: UUID, status: str = "open") -> Ticket:
        ticket = Ticket(user_id=user_id, status=status)
        self.session.add(ticket)
        await self.session.flush()
        await self.session.refresh(ticket)
        return ticket

    async def add_message(self, *, ticket_id: UUID, sender_id: UUID, text: str) -> TicketMessage:
        message = TicketMessage(ticket_id=ticket_id, sender_id=sender_id, text=text)
        self.session.add(message)
        await self.session.flush()
        await self.session.refresh(message)
        return message

    async def get_ticket_with_messages(self, ticket_id: UUID) -> Ticket | None:
        result = await self.session.execute(
            select(Ticket)
            .options(selectinload(Ticket.messages))
            .where(Ticket.id == ticket_id)
        )
        return result.scalar_one_or_none()

    async def set_status(self, ticket: Ticket, status: str) -> Ticket:
        ticket.status = status
        self.session.add(ticket)
        await self.session.flush()
        await self.session.refresh(ticket)
        return ticket
