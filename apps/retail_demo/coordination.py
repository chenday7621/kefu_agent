"""Single-instance session gates and actual native task completion snapshots."""

import asyncio
from collections import defaultdict
from inspect import iscoroutinefunction, signature
from parlant.core.version import VERSION
from .business import BusinessError


class SessionCoordinator:
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)
        self.tasks = None

    def install(self, module, background_tasks):
        original = getattr(module, "_process_session", None)
        dispatch = getattr(module, "dispatch_processing_task", None)
        dispatch_code = getattr(dispatch, "__code__", None)
        if not (
            VERSION == "3.3.2"
            and iscoroutinefunction(original)
            and tuple(signature(original).parameters) == ("session",)
            and iscoroutinefunction(dispatch)
            and tuple(signature(dispatch).parameters) == ("session",)
            and dispatch_code
            and "process-session(" in dispatch_code.co_consts
            and ")" in dispatch_code.co_consts
            and "restart" in dispatch_code.co_names
            and isinstance(getattr(background_tasks, "_tasks", None), dict)
            and getattr(module, "_background_task_service", None) is background_tasks
        ):
            raise RuntimeError(
                "Retail demo session coordination requires audited Parlant 3.3.2 "
                "_process_session(session), process-session({session.id}) task tags "
                "and the same BackgroundTaskService._tasks dictionary. "
                "Re-audit these interfaces before upgrading; refusing to continue."
            )
        self.tasks = background_tasks

        async def process(session):
            async with self.locks[str(session.id)]:
                await original(session)

        # This version has no public processing-completion waiter. Keep this
        # app-local hook explicit; never infer task completion from ready events.
        module._process_session = process

    def processing(self, sid):
        task = self.tasks._tasks.get(f"process-session({sid})") if self.tasks else None
        return task if task and not task.done() else None

    async def snapshot(self, sid, store, timeout=60):
        async def capture():
            while True:
                if task := self.processing(sid):
                    await asyncio.shield(task)
                async with self.locks[sid]:
                    if self.processing(sid):
                        continue
                    async with store._database.connection() as c:
                        await c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        row = await (
                            await c.execute(
                                "SELECT doc,next_offset FROM parlant_sessions WHERE id=%s", (sid,)
                            )
                        ).fetchone()
                        if not row:
                            raise BusinessError("SESSION_NOT_FOUND", "会话不存在。")
                        events = await (
                            await c.execute(
                                "SELECT doc FROM parlant_events WHERE session_id=%s ORDER BY event_offset",
                                (sid,),
                            )
                        ).fetchall()
                        notifications = await (
                            await c.execute(
                                "SELECT id,operation_id,request_id,status,receipt_event_id FROM return_outbox WHERE session_id=%s ORDER BY id",
                                (sid,),
                            )
                        ).fetchall()
                        current = await (
                            await c.execute(
                                "SELECT operation_id,preparation_seq FROM session_current_operations WHERE session_id=%s",
                                (sid,),
                            )
                        ).fetchone()
                        from .business import plain

                        return plain(
                            {
                                "session": row["doc"],
                                "next_offset": row["next_offset"],
                                "events": [e["doc"] for e in events],
                                "current_operation": current,
                                "outbox": notifications,
                                "boundary": "native-processing-task-completed; session-gate; repeatable-read",
                            }
                        )

        return await asyncio.wait_for(capture(), timeout)
