from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase


ModelT = TypeVar("ModelT", bound=DeclarativeBase)


class AsyncRepository(Generic[ModelT]):
    """Small generic repository for async SQLAlchemy 2.0 access."""

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    def _base_query(self) -> Select[tuple[ModelT]]:
        return select(self.model)

    async def get(self, entity_id: Any) -> ModelT | None:
        """Fetch one entity by primary key."""
        return await self.session.get(self.model, entity_id)

    async def create(self, **values: Any) -> ModelT:
        """Create and flush a new entity instance."""
        instance = self.model(**values)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def update(self, instance: ModelT, **values: Any) -> ModelT:
        """Apply field updates to an existing entity and flush them."""
        for field_name, field_value in values.items():
            setattr(instance, field_name, field_value)

        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def get_one_by(self, **filters: Any) -> ModelT | None:
        """Fetch one entity by exact-match filters."""
        query = self._base_query().filter_by(**filters)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_by(self, **filters: Any) -> list[ModelT]:
        """Fetch multiple entities by exact-match filters."""
        query = self._base_query().filter_by(**filters)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def delete(self, instance: ModelT) -> None:
        """Delete an entity and flush the pending change."""
        await self.session.delete(instance)
        await self.session.flush()

    async def exists(self, **filters: Any) -> bool:
        """Check whether at least one row exists for the given filters."""
        query = self._base_query().filter_by(**filters).limit(1)
        result = await self.session.execute(query)
        return result.scalar_one_or_none() is not None

    @staticmethod
    def merge_update_data(
        current_data: Mapping[str, Any],
        new_data: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Utility for safely merging partial update dictionaries."""
        merged = dict(current_data)
        merged.update(new_data)
        return merged
