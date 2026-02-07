import asyncio
import datetime
import math

from .constants import (
    DEFAULT_UPDATE_JOB_STATUS_INTERVAL,
    REFRESH_SLEEP_SECONDS,
)


def get_seconds_delta(seconds):
    return datetime.timedelta(seconds=seconds)


def adjust_sleep_interval(snapshot_interval=None, sleep_interval=None):
    if snapshot_interval is None:
        return sleep_interval
    if snapshot_interval < 1:
        return max(snapshot_interval, 0.0)
    return snapshot_interval / (math.floor(snapshot_interval / 2.0) + 1)


class TaskTimer:
    def __init__(
        self,
        continue_condition=None,
        refresh_fn=None,
        snapshot_interval=None,
        sleep_interval=REFRESH_SLEEP_SECONDS,
        total_time=None,
        loop=None,
    ):
        self._continue_condition = continue_condition
        self._refresh_fn = refresh_fn
        self._refresh_interval = get_seconds_delta(
            snapshot_interval or DEFAULT_UPDATE_JOB_STATUS_INTERVAL
        )
        self._sleep_interval = adjust_sleep_interval(snapshot_interval, sleep_interval)
        self._total_time = (
            datetime.timedelta(seconds=total_time) if total_time is not None else None
        )
        self._loop = loop if loop is not None else asyncio.get_event_loop()

    async def _heartbeat(self):
        start_time = datetime.datetime.now()
        last_refresh = start_time
        refresh_elapsed_time = get_seconds_delta(0)
        ideal_time_elapsed = get_seconds_delta(0)
        try:
            while True:
                loop_start = datetime.datetime.now()
                elapsed_time_from_loop_start = loop_start - start_time
                if (not await self._should_continue()) or (
                    (
                        (
                            elapsed_time_from_loop_start
                            + (datetime.datetime.now() - loop_start)
                            + get_seconds_delta(0.001)
                            >= self._total_time
                        )
                        if self._total_time is not None
                        else False
                    )
                ):
                    break

                adjusted_elapsed_time = loop_start - last_refresh
                if self._refresh_fn is not None and (
                    adjusted_elapsed_time >= self._refresh_interval
                ):
                    self._refresh_fn()
                    last_refresh = loop_start

                adjusted_sleep = (
                    self._refresh_interval - adjusted_elapsed_time
                ).total_seconds()
                if adjusted_sleep > 0:
                    await asyncio.sleep(
                        adjusted_sleep,
                        loop=self._loop,
                    )
        except RuntimeError as exc:
            raise exc
        except Exception as exc:
            raise exc
        finally:
            self.stop()

    async def _should_continue(self):
        if not self._continue:
            return False
        if self._continue_condition is None:
            return True
        return await self._continue_condition()

    async def start(self):
        self._continue = True
        if self._refresh_fn is not None:
            self._refresh_fn()
        await self._heartbeat()

    def stop(self):
        self._continue = False
