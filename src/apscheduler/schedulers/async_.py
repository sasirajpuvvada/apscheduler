from __future__ import annotations

import os
import platform
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import UUID, uuid4

import anyio
from anyio import TASK_STATUS_IGNORED, create_task_group, get_cancelled_exc_class, move_on_after
from anyio.abc import TaskGroup

from ..abc import AsyncDataStore, DataStore, EventSource, Job, Schedule, Trigger
from ..datastores.async_adapter import AsyncDataStoreAdapter
from ..datastores.memory import MemoryDataStore
from ..enums import CoalescePolicy, ConflictPolicy, JobOutcome, RunState
from ..eventbrokers.async_local import LocalAsyncEventBroker
from ..events import (
    JobReleased, ScheduleAdded, SchedulerStarted, SchedulerStopped, ScheduleUpdated)
from ..exceptions import JobCancelled, JobDeadlineMissed, JobLookupError
from ..marshalling import callable_to_ref
from ..structures import JobResult, Task
from ..workers.async_ import AsyncWorker


class AsyncScheduler:
    """An asynchronous (AnyIO based) scheduler implementation."""

    data_store: AsyncDataStore
    _state: RunState = RunState.stopped
    _wakeup_event: anyio.Event
    _worker: Optional[AsyncWorker] = None
    _task_group: Optional[TaskGroup] = None

    def __init__(self, data_store: DataStore | AsyncDataStore | None = None, *,
                 identity: Optional[str] = None, logger: Optional[Logger] = None,
                 start_worker: bool = True):
        self.identity = identity or f'{platform.node()}-{os.getpid()}-{id(self)}'
        self.logger = logger or getLogger(__name__)
        self.start_worker = start_worker
        self._exit_stack = AsyncExitStack()
        self._events = LocalAsyncEventBroker()

        data_store = data_store or MemoryDataStore()
        if isinstance(data_store, DataStore):
            self.data_store = AsyncDataStoreAdapter(data_store)
        else:
            self.data_store = data_store

    @property
    def events(self) -> EventSource:
        return self._events

    @property
    def worker(self) -> Optional[AsyncWorker]:
        return self._worker

    async def __aenter__(self):
        self._state = RunState.starting
        self._wakeup_event = anyio.Event()
        await self._exit_stack.__aenter__()
        await self._exit_stack.enter_async_context(self._events)

        # Initialize the data store and start relaying events to the scheduler's event broker
        await self._exit_stack.enter_async_context(self.data_store)
        self._exit_stack.enter_context(self.data_store.events.subscribe(self._events.publish))

        # Wake up the scheduler if the data store emits a significant schedule event
        self._exit_stack.enter_context(
            self.data_store.events.subscribe(
                lambda event: self._wakeup_event.set(), {ScheduleAdded, ScheduleUpdated}
            )
        )

        # Start the built-in worker, if configured to do so
        if self.start_worker:
            self._worker = AsyncWorker(self.data_store)
            await self._exit_stack.enter_async_context(self._worker)

        # Start the worker and return when it has signalled readiness or raised an exception
        self._task_group = create_task_group()
        await self._exit_stack.enter_async_context(self._task_group)
        await self._task_group.start(self.run)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._state = RunState.stopping
        self._wakeup_event.set()
        await self._exit_stack.__aexit__(exc_type, exc_val, exc_tb)
        del self._task_group
        del self._wakeup_event

    async def add_schedule(
        self, func_or_task_id: str | Callable, trigger: Trigger, *, id: Optional[str] = None,
        args: Optional[Iterable] = None, kwargs: Optional[Mapping[str, Any]] = None,
        coalesce: CoalescePolicy = CoalescePolicy.latest,
        misfire_grace_time: float | timedelta | None = None, tags: Optional[Iterable[str]] = None,
        conflict_policy: ConflictPolicy = ConflictPolicy.do_nothing
    ) -> str:
        id = id or str(uuid4())
        args = tuple(args or ())
        kwargs = dict(kwargs or {})
        tags = frozenset(tags or ())
        if isinstance(misfire_grace_time, (int, float)):
            misfire_grace_time = timedelta(seconds=misfire_grace_time)

        if callable(func_or_task_id):
            task = Task(id=callable_to_ref(func_or_task_id), func=func_or_task_id)
            await self.data_store.add_task(task)
        else:
            task = await self.data_store.get_task(func_or_task_id)

        schedule = Schedule(id=id, task_id=task.id, trigger=trigger, args=args, kwargs=kwargs,
                            coalesce=coalesce, misfire_grace_time=misfire_grace_time, tags=tags)
        schedule.next_fire_time = trigger.next()
        await self.data_store.add_schedule(schedule, conflict_policy)
        self.logger.info('Added new schedule (task=%r, trigger=%r); next run time at %s', task,
                         trigger, schedule.next_fire_time)
        return schedule.id

    async def remove_schedule(self, schedule_id: str) -> None:
        await self.data_store.remove_schedules({schedule_id})

    async def add_job(
        self, func_or_task_id: str | Callable, *, args: Optional[Iterable] = None,
        kwargs: Optional[Mapping[str, Any]] = None, tags: Optional[Iterable[str]] = None
    ) -> UUID:
        """
        Add a job to the data store.

        :param func_or_task_id:
        :param args: positional arguments to call the target callable with
        :param kwargs: keyword arguments to call the target callable with
        :param tags:
        :return: the ID of the newly created job

        """
        if callable(func_or_task_id):
            task = Task(id=callable_to_ref(func_or_task_id), func=func_or_task_id)
            await self.data_store.add_task(task)
        else:
            task = await self.data_store.get_task(func_or_task_id)

        job = Job(task_id=task.id, args=args, kwargs=kwargs, tags=tags)
        await self.data_store.add_job(job)
        return job.id

    async def get_job_result(self, job_id: UUID, *, wait: bool = True) -> JobResult:
        """
        Retrieve the result of a job.

        :param job_id: the ID of the job
        :param wait: if ``True``, wait until the job has ended (one way or another), ``False`` to
                     raise an exception if the result is not yet available
        :raises JobLookupError: if the job does not exist in the data store

        """
        wait_event = anyio.Event()

        def listener(event: JobReleased) -> None:
            if event.job_id == job_id:
                wait_event.set()

        with self.data_store.events.subscribe(listener, {JobReleased}):
            result = await self.data_store.get_job_result(job_id)
            if result:
                return result
            elif not wait:
                raise JobLookupError(job_id)

            await wait_event.wait()

        result = await self.data_store.get_job_result(job_id)
        assert isinstance(result, JobResult)
        return result

    async def run_job(
        self, func_or_task_id: str | Callable, *, args: Optional[Iterable] = None,
        kwargs: Optional[Mapping[str, Any]] = None, tags: Optional[Iterable[str]] = ()
    ) -> Any:
        """
        Convenience method to add a job and then return its result (or raise its exception).

        :returns: the return value of the target function

        """
        job_complete_event = anyio.Event()

        def listener(event: JobReleased) -> None:
            if event.job_id == job_id:
                job_complete_event.set()

        job_id: Optional[UUID] = None
        with self.data_store.events.subscribe(listener, {JobReleased}):
            job_id = await self.add_job(func_or_task_id, args=args, kwargs=kwargs, tags=tags)
            await job_complete_event.wait()

        result = await self.get_job_result(job_id)
        if result.outcome is JobOutcome.success:
            return result.return_value
        elif result.outcome is JobOutcome.error:
            raise result.exception
        elif result.outcome is JobOutcome.missed_start_deadline:
            raise JobDeadlineMissed
        elif result.outcome is JobOutcome.cancelled:
            raise JobCancelled
        else:
            raise RuntimeError(f'Unknown job outcome: {result.outcome}')

    async def run(self, *, task_status=TASK_STATUS_IGNORED) -> None:
        if self._state is not RunState.starting:
            raise RuntimeError(f'This function cannot be called while the scheduler is in the '
                               f'{self._state} state')

        # Signal that the scheduler has started
        self._state = RunState.started
        task_status.started()
        await self._events.publish(SchedulerStarted())

        try:
            while self._state is RunState.started:
                schedules = await self.data_store.acquire_schedules(self.identity, 100)
                now = datetime.now(timezone.utc)
                for schedule in schedules:
                    # Calculate a next fire time for the schedule, if possible
                    fire_times = [schedule.next_fire_time]
                    calculate_next = schedule.trigger.next
                    while True:
                        try:
                            fire_time = calculate_next()
                        except Exception:
                            self.logger.exception(
                                'Error computing next fire time for schedule %r of task %r – '
                                'removing schedule', schedule.id, schedule.task_id)
                            break

                        # Stop if the calculated fire time is in the future
                        if fire_time is None or fire_time > now:
                            schedule.next_fire_time = fire_time
                            break

                        # Only keep all the fire times if coalesce policy = "all"
                        if schedule.coalesce is CoalescePolicy.all:
                            fire_times.append(fire_time)
                        elif schedule.coalesce is CoalescePolicy.latest:
                            fire_times[0] = fire_time

                    # Add one or more jobs to the job queue
                    for fire_time in fire_times:
                        schedule.last_fire_time = fire_time
                        job = Job(task_id=schedule.task_id, args=schedule.args,
                                  kwargs=schedule.kwargs, schedule_id=schedule.id,
                                  scheduled_fire_time=fire_time,
                                  start_deadline=schedule.next_deadline, tags=schedule.tags)
                        await self.data_store.add_job(job)

                    # Update the schedules (and release the scheduler's claim on them)
                    await self.data_store.release_schedules(self.identity, schedules)

                # If we received fewer schedules than the maximum amount, sleep until the next
                # schedule is due or the scheduler is explicitly woken up
                wait_time = None
                if len(schedules) < 100:
                    next_fire_time = await self.data_store.get_next_schedule_run_time()
                    if next_fire_time:
                        wait_time = (datetime.now(timezone.utc) - next_fire_time).total_seconds()

                with move_on_after(wait_time):
                    await self._wakeup_event.wait()

                self._wakeup_event = anyio.Event()
        except get_cancelled_exc_class():
            pass
        except BaseException as exc:
            self._state = RunState.stopped
            await self._events.publish(SchedulerStopped(exception=exc))
            raise

        self._state = RunState.stopped
        await self._events.publish(SchedulerStopped())
