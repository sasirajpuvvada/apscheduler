from __future__ import annotations

import os
import platform
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import UUID, uuid4

from ..abc import DataStore, EventSource, Trigger
from ..datastores.memory import MemoryDataStore
from ..enums import CoalescePolicy, ConflictPolicy, JobOutcome, RunState
from ..eventbrokers.local import LocalEventBroker
from ..events import (
    Event, JobReleased, ScheduleAdded, SchedulerStarted, SchedulerStopped, ScheduleUpdated)
from ..exceptions import JobCancelled, JobDeadlineMissed, JobLookupError
from ..marshalling import callable_to_ref
from ..structures import Job, JobResult, Schedule, Task
from ..workers.sync import Worker


class Scheduler:
    """A synchronous scheduler implementation."""

    _state: RunState = RunState.stopped
    _wakeup_event: threading.Event
    _worker: Optional[Worker] = None

    def __init__(self, data_store: Optional[DataStore] = None, *, identity: Optional[str] = None,
                 logger: Optional[Logger] = None, start_worker: bool = True):
        self.identity = identity or f'{platform.node()}-{os.getpid()}-{id(self)}'
        self.logger = logger or getLogger(__name__)
        self.start_worker = start_worker
        self.data_store = data_store or MemoryDataStore()
        self._exit_stack = ExitStack()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._events = LocalEventBroker()

    @property
    def events(self) -> EventSource:
        return self._events

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def worker(self) -> Optional[Worker]:
        return self._worker

    def __enter__(self) -> Scheduler:
        self._state = RunState.starting
        self._wakeup_event = threading.Event()
        self._exit_stack.__enter__()
        self._exit_stack.enter_context(self._events)

        # Initialize the data store and start relaying events to the scheduler's event broker
        self._exit_stack.enter_context(self.data_store)
        self._exit_stack.enter_context(self.data_store.events.subscribe(self._events.publish))

        # Wake up the scheduler if the data store emits a significant schedule event
        self._exit_stack.enter_context(
            self.data_store.events.subscribe(
                lambda event: self._wakeup_event.set(), {ScheduleAdded, ScheduleUpdated}
            )
        )

        # Start the built-in worker, if configured to do so
        if self.start_worker:
            self._worker = Worker(self.data_store)
            self._exit_stack.enter_context(self._worker)

        # Start the scheduler and return when it has signalled readiness or raised an exception
        start_future: Future[Event] = Future()
        with self._events.subscribe(start_future.set_result, one_shot=True):
            run_future = self._executor.submit(self.run)
            wait([start_future, run_future], return_when=FIRST_COMPLETED)

        if run_future.done():
            run_future.result()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._state = RunState.stopping
        self._wakeup_event.set()
        self._executor.shutdown(wait=exc_type is None)
        self._exit_stack.__exit__(exc_type, exc_val, exc_tb)
        del self._wakeup_event

    def add_schedule(
        self, func_or_task_id: str | Callable, trigger: Trigger, *, id: Optional[str] = None,
        args: Optional[Iterable] = None, kwargs: Optional[Mapping[str, Any]] = None,
        coalesce: CoalescePolicy = CoalescePolicy.latest,
        misfire_grace_time: float | timedelta | None = None,
        tags: Optional[Iterable[str]] = None,
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
            self.data_store.add_task(task)
        else:
            task = self.data_store.get_task(func_or_task_id)

        schedule = Schedule(id=id, task_id=task.id, trigger=trigger, args=args, kwargs=kwargs,
                            coalesce=coalesce, misfire_grace_time=misfire_grace_time, tags=tags)
        schedule.next_fire_time = trigger.next()
        self.data_store.add_schedule(schedule, conflict_policy)
        self.logger.info('Added new schedule (task=%r, trigger=%r); next run time at %s', task,
                         trigger, schedule.next_fire_time)
        return schedule.id

    def remove_schedule(self, schedule_id: str) -> None:
        self.data_store.remove_schedules({schedule_id})

    def add_job(
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
            self.data_store.add_task(task)
        else:
            task = self.data_store.get_task(func_or_task_id)

        job = Job(task_id=task.id, args=args, kwargs=kwargs, tags=tags)
        self.data_store.add_job(job)
        return job.id

    def get_job_result(self, job_id: UUID, *, wait: bool = True) -> JobResult:
        """
        Retrieve the result of a job.

        :param job_id: the ID of the job
        :param wait: if ``True``, wait until the job has ended (one way or another), ``False`` to
                     raise an exception if the result is not yet available
        :raises JobLookupError: if the job does not exist in the data store

        """
        wait_event = threading.Event()

        def listener(event: JobReleased) -> None:
            if event.job_id == job_id:
                wait_event.set()

        with self.data_store.events.subscribe(listener, {JobReleased}):
            result = self.data_store.get_job_result(job_id)
            if result:
                return result
            elif not wait:
                raise JobLookupError(job_id)

            wait_event.wait()

        result = self.data_store.get_job_result(job_id)
        assert isinstance(result, JobResult)
        return result

    def run_job(
        self, func_or_task_id: str | Callable, *, args: Optional[Iterable] = None,
        kwargs: Optional[Mapping[str, Any]] = None, tags: Optional[Iterable[str]] = ()
    ) -> Any:
        """
        Convenience method to add a job and then return its result (or raise its exception).

        :returns: the return value of the target function

        """
        job_complete_event = threading.Event()

        def listener(event: JobReleased) -> None:
            if event.job_id == job_id:
                job_complete_event.set()

        job_id: Optional[UUID] = None
        with self.data_store.events.subscribe(listener, {JobReleased}):
            job_id = self.add_job(func_or_task_id, args=args, kwargs=kwargs, tags=tags)
            job_complete_event.wait()

        result = self.get_job_result(job_id)
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

    def run(self) -> None:
        if self._state is not RunState.starting:
            raise RuntimeError(f'This function cannot be called while the scheduler is in the '
                               f'{self._state} state')

        # Signal that the scheduler has started
        self._state = RunState.started
        self._events.publish(SchedulerStarted())

        try:
            while self._state is RunState.started:
                schedules = self.data_store.acquire_schedules(self.identity, 100)
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
                        self.data_store.add_job(job)

                # Update the schedules (and release the scheduler's claim on them)
                self.data_store.release_schedules(self.identity, schedules)

                # If we received fewer schedules than the maximum amount, sleep until the next
                # schedule is due or the scheduler is explicitly woken up
                wait_time = None
                if len(schedules) < 100:
                    next_fire_time = self.data_store.get_next_schedule_run_time()
                    if next_fire_time:
                        wait_time = (datetime.now(timezone.utc) - next_fire_time).total_seconds()

                if self._wakeup_event.wait(wait_time):
                    self._wakeup_event = threading.Event()
        except BaseException as exc:
            self._state = RunState.stopped
            self._events.publish(SchedulerStopped(exception=exc))
            raise

        self._state = RunState.stopped
        self._events.publish(SchedulerStopped())

    # def stop(self) -> None:
    #     self.portal.call(self._scheduler.stop)
    #
    # def wait_until_stopped(self) -> None:
    #     self.portal.call(self._scheduler.wait_until_stopped)
