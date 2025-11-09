"""Background task that automatically enqueues builds when refs advance."""

from __future__ import annotations

import asyncio

from sqlmodel import Session, select

from .config import settings
from .database import engine
from .git_utils import GitError, get_remote_sha
from .models import Build, BuildStatus, RefType, Repository, TrackedTarget
from .build_service import BuildQueue, enqueue_target_build


class AutoBuildMonitor:
    """Periodic watcher that checks tracked targets for new commits."""

    def __init__(self, queue: BuildQueue) -> None:
        """Store the queue used to enqueue builds."""
        self.queue = queue
        self.task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """Launch the monitoring loop if it is not already running."""
        if self.task:
            return
        self.task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        """Stop the monitoring loop gracefully."""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _loop(self) -> None:
        """Sleep for the configured interval then scan tracked targets."""
        interval = max(10, settings.auto_build_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            await self._check_targets()

    async def _check_targets(self) -> None:
        """Inspect every auto-build-enabled target and queue builds if needed."""
        with Session(engine) as session:
            targets = session.exec(select(TrackedTarget).where(TrackedTarget.auto_build == True)).all()
            for target in targets:
                repo = session.get(Repository, target.repository_id)
                if not repo:
                    continue
                pending = session.exec(
                    select(Build)
                    .where(
                        (Build.target_id == target.id)
                        & (Build.status.in_([BuildStatus.queued, BuildStatus.running]))
                    )
                    .limit(1)
                ).first()
                if pending:
                    continue

                try:
                    remote_sha = await asyncio.to_thread(
                        get_remote_sha,
                        repo.url,
                        repo.auth_token,
                        target.ref_type,
                        target.ref_name,
                        repo.deploy_key,
                    )
                except GitError:
                    continue
                if not remote_sha or remote_sha == target.last_sha:
                    continue
                await enqueue_target_build(target.id, session, self.queue, triggered_by="auto")
