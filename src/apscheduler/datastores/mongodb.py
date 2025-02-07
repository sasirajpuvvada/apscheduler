from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from logging import Logger, getLogger
from typing import ClassVar, Iterable, Optional
from uuid import UUID

import attr
import pymongo
from attr.validators import instance_of
from pymongo import ASCENDING, DeleteOne, MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from ..abc import DataStore, EventBroker, EventSource, Job, Schedule, Serializer
from ..enums import ConflictPolicy
from ..eventbrokers.local import LocalEventBroker
from ..events import (
    DataStoreEvent, JobAcquired, JobAdded, JobReleased, ScheduleAdded, ScheduleRemoved,
    ScheduleUpdated, TaskAdded, TaskRemoved, TaskUpdated)
from ..exceptions import (
    ConflictingIdError, DeserializationError, SerializationError, TaskLookupError)
from ..serializers.pickle import PickleSerializer
from ..structures import JobResult, Task
from ..util import reentrant


@reentrant
@attr.define(eq=False)
class MongoDBDataStore(DataStore):
    client: MongoClient = attr.field(validator=instance_of(MongoClient))
    serializer: Serializer = attr.field(factory=PickleSerializer, kw_only=True)
    database: str = attr.field(default='apscheduler', kw_only=True)
    lock_expiration_delay: float = attr.field(default=30, kw_only=True)
    start_from_scratch: bool = attr.field(default=False, kw_only=True)

    _task_attrs: ClassVar[list[str]] = [field.name for field in attr.fields(Task)]
    _schedule_attrs: ClassVar[list[str]] = [field.name for field in attr.fields(Schedule)]
    _job_attrs: ClassVar[list[str]] = [field.name for field in attr.fields(Job)]

    _logger: Logger = attr.field(init=False, factory=lambda: getLogger(__name__))
    _exit_stack: ExitStack = attr.field(init=False, factory=ExitStack)
    _events: EventBroker = attr.field(init=False, factory=LocalEventBroker)
    _local_tasks: dict[str, Task] = attr.field(init=False, factory=dict)

    @client.validator
    def validate_client(self, attribute: attr.Attribute, value: MongoClient) -> None:
        if not value.delegate.codec_options.tz_aware:
            raise ValueError('MongoDB client must have tz_aware set to True')

    def __attrs_post_init__(self) -> None:
        database = self.client[self.database]
        self._tasks: Collection = database['tasks']
        self._schedules: Collection = database['schedules']
        self._jobs: Collection = database['jobs']
        self._jobs_results: Collection = database['job_results']

    @classmethod
    def from_url(cls, uri: str, **options) -> MongoDBDataStore:
        client = MongoClient(uri)
        return cls(client, **options)

    @property
    def events(self) -> EventSource:
        return self._events

    def __enter__(self):
        server_info = self.client.server_info()
        if server_info['versionArray'] < [4, 0]:
            raise RuntimeError(f"MongoDB server must be at least v4.0; current version = "
                               f"{server_info['version']}")

        self._exit_stack.__enter__()
        self._exit_stack.enter_context(self._events)

        if self.start_from_scratch:
            self._tasks.delete_many({})
            self._schedules.delete_many({})
            self._jobs.delete_many({})
            self._jobs_results.delete_many({})

        self._schedules.create_index('next_fire_time')
        self._jobs.create_index('task_id')
        self._jobs.create_index('created_at')
        self._jobs.create_index('tags')
        self._jobs_results.create_index('finished_at')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._exit_stack.__exit__(exc_type, exc_val, exc_tb)

    def add_task(self, task: Task) -> None:
        previous = self._tasks.find_one_and_update(
            {'_id': task.id},
            {'$set': task.marshal(self.serializer),
             '$setOnInsert': {'running_jobs': 0}},
            upsert=True
        )
        self._local_tasks[task.id] = task
        if previous:
            self._events.publish(TaskUpdated(task_id=task.id))
        else:
            self._events.publish(TaskAdded(task_id=task.id))

    def remove_task(self, task_id: str) -> None:
        if not self._tasks.find_one_and_delete({'_id': task_id}):
            raise TaskLookupError(task_id)

        del self._local_tasks[task_id]
        self._events.publish(TaskRemoved(task_id=task_id))

    def get_task(self, task_id: str) -> Task:
        try:
            return self._local_tasks[task_id]
        except KeyError:
            document = self._tasks.find_one({'_id': task_id}, projection=self._task_attrs)
            if not document:
                raise TaskLookupError(task_id)

            document['id'] = document.pop('id')
            task = self._local_tasks[task_id] = Task.unmarshal(self.serializer, document)
            return task

    def get_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        for document in self._tasks.find(projection=self._task_attrs,
                                         sort=[('_id', pymongo.ASCENDING)]):
            document['id'] = document.pop('_id')
            tasks.append(Task.unmarshal(self.serializer, document))

        return tasks

    def get_schedules(self, ids: Optional[set[str]] = None) -> list[Schedule]:
        schedules: list[Schedule] = []
        filters = {'_id': {'$in': list(ids)}} if ids is not None else {}
        cursor = self._schedules.find(filters).sort('_id')
        for document in cursor:
            document['id'] = document.pop('_id')
            try:
                schedule = Schedule.unmarshal(self.serializer, document)
            except DeserializationError:
                self._logger.warning('Failed to deserialize schedule %r', document['_id'])
                continue

            schedules.append(schedule)

        return schedules

    def add_schedule(self, schedule: Schedule, conflict_policy: ConflictPolicy) -> None:
        event: DataStoreEvent
        document = schedule.marshal(self.serializer)
        document['_id'] = document.pop('id')
        try:
            self._schedules.insert_one(document)
        except DuplicateKeyError:
            if conflict_policy is ConflictPolicy.exception:
                raise ConflictingIdError(schedule.id) from None
            elif conflict_policy is ConflictPolicy.replace:
                self._schedules.replace_one({'_id': schedule.id}, document, True)
                event = ScheduleUpdated(
                    schedule_id=schedule.id,
                    next_fire_time=schedule.next_fire_time)
                self._events.publish(event)
        else:
            event = ScheduleAdded(schedule_id=schedule.id,
                                  next_fire_time=schedule.next_fire_time)
            self._events.publish(event)

    def remove_schedules(self, ids: Iterable[str]) -> None:
        with self.client.start_session() as s, s.start_transaction():
            filters = {'_id': {'$in': list(ids)}} if ids is not None else {}
            cursor = self._schedules.find(filters, projection=['_id'])
            ids = [doc['_id'] for doc in cursor]
            if ids:
                self._schedules.delete_many(filters)

        for schedule_id in ids:
            self._events.publish(ScheduleRemoved(schedule_id=schedule_id))

    def acquire_schedules(self, scheduler_id: str, limit: int) -> list[Schedule]:
        schedules: list[Schedule] = []
        with self.client.start_session() as s, s.start_transaction():
            cursor = self._schedules.find(
                {'next_fire_time': {'$ne': None},
                 '$or': [{'acquired_until': {'$exists': False}},
                         {'acquired_until': {'$lt': datetime.now(timezone.utc)}}]
                 }
            ).sort('next_fire_time').limit(limit)
            for document in cursor:
                document['id'] = document.pop('_id')
                schedule = Schedule.unmarshal(self.serializer, document)
                schedules.append(schedule)

            if schedules:
                now = datetime.now(timezone.utc)
                acquired_until = datetime.fromtimestamp(
                    now.timestamp() + self.lock_expiration_delay, now.tzinfo)
                filters = {'_id': {'$in': [schedule.id for schedule in schedules]}}
                update = {'$set': {'acquired_by': scheduler_id,
                                   'acquired_until': acquired_until}}
                self._schedules.update_many(filters, update)

        return schedules

    def release_schedules(self, scheduler_id: str, schedules: list[Schedule]) -> None:
        updated_schedules: list[tuple[str, datetime]] = []
        finished_schedule_ids: list[str] = []
        with self.client.start_session() as s, s.start_transaction():
            # Update schedules that have a next fire time
            requests = []
            for schedule in schedules:
                filters = {'_id': schedule.id, 'acquired_by': scheduler_id}
                if schedule.next_fire_time is not None:
                    try:
                        serialized_trigger = self.serializer.serialize(schedule.trigger)
                    except SerializationError:
                        self._logger.exception('Error serializing schedule %r – '
                                               'removing from data store', schedule.id)
                        requests.append(DeleteOne(filters))
                        finished_schedule_ids.append(schedule.id)
                        continue

                    update = {
                        '$unset': {
                            'acquired_by': True,
                            'acquired_until': True,
                        },
                        '$set': {
                            'trigger': serialized_trigger,
                            'next_fire_time': schedule.next_fire_time
                        }
                    }
                    requests.append(UpdateOne(filters, update))
                    updated_schedules.append((schedule.id, schedule.next_fire_time))
                else:
                    requests.append(DeleteOne(filters))
                    finished_schedule_ids.append(schedule.id)

            if requests:
                self._schedules.bulk_write(requests, ordered=False)
                for schedule_id, next_fire_time in updated_schedules:
                    event = ScheduleUpdated(schedule_id=schedule_id, next_fire_time=next_fire_time)
                    self._events.publish(event)

        for schedule_id in finished_schedule_ids:
            self._events.publish(ScheduleRemoved(schedule_id=schedule_id))

    def get_next_schedule_run_time(self) -> Optional[datetime]:
        document = self._schedules.find_one({'next_run_time': {'$ne': None}},
                                            projection=['next_run_time'],
                                            sort=[('next_run_time', ASCENDING)])
        if document:
            return document['next_run_time']
        else:
            return None

    def add_job(self, job: Job) -> None:
        document = job.marshal(self.serializer)
        document['_id'] = document.pop('id')
        self._jobs.insert_one(document)
        event = JobAdded(job_id=job.id, task_id=job.task_id, schedule_id=job.schedule_id,
                         tags=job.tags)
        self._events.publish(event)

    def get_jobs(self, ids: Optional[Iterable[UUID]] = None) -> list[Job]:
        jobs: list[Job] = []
        filters = {'_id': {'$in': list(ids)}} if ids is not None else {}
        cursor = self._jobs.find(filters).sort('_id')
        for document in cursor:
            document['id'] = document.pop('_id')
            try:
                job = Job.unmarshal(self.serializer, document)
            except DeserializationError:
                self._logger.warning('Failed to deserialize job %r', document['id'])
                continue

            jobs.append(job)

        return jobs

    def acquire_jobs(self, worker_id: str, limit: Optional[int] = None) -> list[Job]:
        with self.client.start_session() as session:
            cursor = self._jobs.find(
                {'$or': [{'acquired_until': {'$exists': False}},
                         {'acquired_until': {'$lt': datetime.now(timezone.utc)}}]
                 },
                sort=[('created_at', ASCENDING)],
                limit=limit,
                session=session
            )
            documents = list(cursor)

            # Retrieve the limits
            task_ids: set[str] = {document['task_id'] for document in documents}
            task_limits = self._tasks.find(
                {'_id': {'$in': list(task_ids)}, 'max_running_jobs': {'$ne': None}},
                projection=['max_running_jobs', 'running_jobs'],
                session=session
            )
            job_slots_left = {doc['_id']: doc['max_running_jobs'] - doc['running_jobs']
                              for doc in task_limits}

            # Filter out jobs that don't have free slots
            acquired_jobs: list[Job] = []
            increments: dict[str, int] = defaultdict(lambda: 0)
            for document in documents:
                document['id'] = document.pop('_id')
                job = Job.unmarshal(self.serializer, document)

                # Don't acquire the job if there are no free slots left
                slots_left = job_slots_left.get(job.task_id)
                if slots_left == 0:
                    continue
                elif slots_left is not None:
                    job_slots_left[job.task_id] -= 1

                acquired_jobs.append(job)
                increments[job.task_id] += 1

            if acquired_jobs:
                now = datetime.now(timezone.utc)
                acquired_until = datetime.fromtimestamp(
                    now.timestamp() + self.lock_expiration_delay, timezone.utc)
                filters = {'_id': {'$in': [job.id for job in acquired_jobs]}}
                update = {'$set': {'acquired_by': worker_id,
                                   'acquired_until': acquired_until}}
                self._jobs.update_many(filters, update, session=session)

                # Increment the running job counters on each task
                for task_id, increment in increments.items():
                    self._tasks.find_one_and_update(
                        {'_id': task_id},
                        {'$inc': {'running_jobs': increment}},
                        session=session
                    )

                # Publish the appropriate events
                for job in acquired_jobs:
                    self._events.publish(JobAcquired(job_id=job.id, worker_id=worker_id))

            return acquired_jobs

    def release_job(self, worker_id: str, task_id: str, result: JobResult) -> None:
        with self.client.start_session() as session:
            # Insert the job result
            document = result.marshal(self.serializer)
            document['_id'] = document.pop('job_id')
            self._jobs_results.insert_one(document, session=session)

            # Decrement the running jobs counter
            self._tasks.find_one_and_update(
                {'_id': task_id},
                {'$inc': {'running_jobs': -1}},
                session=session
            )

            # Delete the job
            self._jobs.delete_one({'_id': result.job_id}, session=session)

        # Publish the event
        self._events.publish(
            JobReleased(job_id=result.job_id, worker_id=worker_id, outcome=result.outcome)
        )

    def get_job_result(self, job_id: UUID) -> Optional[JobResult]:
        document = self._jobs_results.find_one_and_delete({'_id': job_id})
        if document:
            document['job_id'] = document.pop('_id')
            return JobResult.unmarshal(self.serializer, document)
        else:
            return None
