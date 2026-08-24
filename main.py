import asyncio
import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.cron.manager import CronJobSchedulingError
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType


PLUGIN_NAME = "astrbot_plugin_relay"


@register("relay_system", "User", "多功能传话插件（原生 Cron 持久化版）", "5.1.0")
class RelayPlugin(Star):
    """传话插件。

    周期和单次任务使用 AstrBot 原生 Basic Cron，持久化在 cron_jobs，
    因而可由 AstrBot 页面统一查看、启停、编辑和删除。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.sent_messages: list[dict] = []
        self._background_tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._update_tokenizer()
        self._register_web_api()

    def _register_web_api(self):
        register = getattr(self.context, "register_web_api", None)
        if not register:
            logger.warning("relay: current AstrBot does not expose register_web_api")
            return
        register(f"/{PLUGIN_NAME}/jobs", self._web_list_jobs, ["GET"], "List relay Cron jobs")
        register(f"/{PLUGIN_NAME}/jobs/create", self._web_create_job, ["POST"], "Create relay Cron job")
        register(f"/{PLUGIN_NAME}/jobs/toggle", self._web_toggle_job, ["POST"], "Toggle relay Cron job")
        register(f"/{PLUGIN_NAME}/jobs/delete", self._web_delete_job, ["POST"], "Delete relay Cron job")
        register(f"/{PLUGIN_NAME}/jobs/run", self._web_run_job, ["POST"], "Run relay Cron job now")

    @staticmethod
    def _web_datetime(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    async def _web_owned_job(self, job_id: str):
        cron = getattr(self.context, "cron_manager", None)
        if cron is None:
            return None, "AstrBot Cron 管理器不可用。"
        job = await cron.db.get_cron_job(str(job_id).strip())
        if not job:
            return None, "任务不存在。"
        payload = job.payload if isinstance(job.payload, dict) else {}
        if job.job_type != "basic" or payload.get("relay_plugin") != PLUGIN_NAME:
            return None, "任务不属于传话插件。"
        return job, None

    def _web_job_dict(self, job):
        payload = job.payload if isinstance(job.payload, dict) else {}
        cron = getattr(self.context, "cron_manager", None)
        next_run = cron.get_next_run_time(job.job_id) if cron else None
        return {
            "id": job.job_id,
            "name": job.name,
            "description": job.description or "",
            "expression": job.cron_expression or "",
            "timezone": job.timezone or "",
            "enabled": bool(job.enabled),
            "status": job.status or "idle",
            "next_run": self._web_datetime(next_run or job.next_run_time),
            "last_run": self._web_datetime(job.last_run_at),
            "last_error": job.last_error or "",
            "target_type": payload.get("target_type", ""),
            "target_id": str(payload.get("target_id", "")),
            "content": str(payload.get("content", "")),
            "platform_name": payload.get("platform_name", "aiocqhttp"),
        }

    async def _web_list_jobs(self):
        cron = getattr(self.context, "cron_manager", None)
        if cron is None:
            return error_response("AstrBot Cron 管理器不可用。", status_code=503)
        jobs = await cron.list_jobs("basic")
        result = []
        for job in jobs:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if payload.get("relay_plugin") == PLUGIN_NAME:
                result.append(self._web_job_dict(job))
        result.sort(key=lambda item: (not item["enabled"], item["next_run"] or "9999"))
        return json_response({"jobs": result, "count": len(result)})

    async def _web_create_job(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求数据无效。", status_code=400)
        try:
            target_id = str(payload.get("target_id", "")).strip()
            target_type = str(payload.get("target_type", "private")).strip()
            content = str(payload.get("content", "")).strip()
            hour = int(payload.get("daily_hour", -1))
            minute = int(payload.get("daily_minute", -1))
            day = int(payload.get("day_of_week", -1))
            platform = str(payload.get("platform_name", self.config.get("default_platform", "aiocqhttp"))).strip()
            if not target_id or not content:
                raise ValueError("目标和正文不能为空。")
            if platform:
                self.config["default_platform"] = platform
            result = await self._create_cron(None, target_id, target_type, content, hour, minute, day)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        if result.startswith("已创建") or result.startswith("检测到相同"):
            return json_response({"ok": True, "message": result})
        return error_response(result, status_code=400)

    async def _web_toggle_job(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求数据无效。", status_code=400)
        job, error = await self._web_owned_job(str(payload.get("job_id", "")))
        if error:
            return error_response(error, status_code=404)
        cron = self.context.cron_manager
        updated = await cron.update_job(job.job_id, enabled=bool(payload.get("enabled", False)))
        return json_response({"ok": True, "job": self._web_job_dict(updated)})

    async def _web_delete_job(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求数据无效。", status_code=400)
        job, error = await self._web_owned_job(str(payload.get("job_id", "")))
        if error:
            return error_response(error, status_code=404)
        await self.context.cron_manager.delete_job(job.job_id)
        return json_response({"ok": True, "job_id": job.job_id})

    async def _web_run_job(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求数据无效。", status_code=400)
        job, error = await self._web_owned_job(str(payload.get("job_id", "")))
        if error:
            return error_response(error, status_code=404)
        await self.context.cron_manager.run_job_now(job.job_id)
        return json_response({"ok": True, "job_id": job.job_id})

    async def initialize(self):
        cron = getattr(self.context, "cron_manager", None)
        if cron is None:
            logger.error("relay: AstrBot Cron manager unavailable")
            return
        jobs = await cron.list_jobs("basic")
        restored = 0
        for job in jobs:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if payload.get("relay_plugin") != "astrbot_plugin_relay":
                continue
            # Basic handler registry is process-local; rebind it after reload.
            cron._basic_handlers[job.job_id] = self._run_cron_job
            try:
                cron._schedule_job(job)
                restored += 1
            except Exception:
                logger.exception("relay: failed to restore Cron job %s", job.job_id)
        logger.info("relay: restored %d persistent Cron jobs", restored)

    async def terminate(self):
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        logger.info("relay: terminated; persistent Cron jobs remain in AstrBot")

    def _track(self, task: asyncio.Task):
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _allowed(self, event):
        raw = self.config.get("whitelist", [])
        ids = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()] if isinstance(raw, str) else [str(x).strip() for x in raw if str(x).strip()]
        if not ids:
            return True
        return str(event.get_sender_id()) in ids

    def _denied(self):
        text = str(self.config.get("permission_denied_msg", "⚠️ 权限拒绝：您不在允许使用传话功能的白名单中。"))
        return text if text.strip() else ""

    def _resolve_target(self, target: str) -> str:
        aliases = self.config.get("aliases", [])
        mapping = {}
        if isinstance(aliases, dict):
            for key, value in aliases.items():
                mapping[str(key).strip()] = str(value).strip()
        else:
            for item in aliases:
                text = str(item)
                if "=" not in text:
                    continue
                left, right = text.split("=", 1)
                left = left.replace(";", ":").split(":")[-1].strip()
                mapping[right.strip()] = left
        return mapping.get(target.strip(), target.strip())

    def _update_tokenizer(self):
        triggers = self.config.get("split_triggers", [",", "，", "。", "！", "？", "；", "\\n", ".", "!", "?", ";"])
        chars = []
        for item in triggers:
            if item not in ("\\n", "\n") and item:
                chars.append(re.escape(str(item)))
        self._split_re = re.compile("(?:[" + "".join(chars or ["。！？；.,!?;"]) + "])+")

    def _render(self, text: str) -> str:
        text = re.sub(r"<thinking>.*?</thinking>|<think>.*?</think>", "", str(text), flags=re.I | re.S)
        now = datetime.now()
        return (text.replace("{datetime}", now.strftime("%Y-%m-%d %H:%M:%S"))
                .replace("{date}", now.strftime("%Y-%m-%d"))
                .replace("{time}", now.strftime("%H:%M:%S")).strip())

    def _split_text(self, text: str) -> list[str]:
        if not self.config.get("enable_smart_split", True):
            return [text]
        maximum = int(self.config.get("max_text_length", 150) or 0)
        if maximum > 0 and len(text) > maximum:
            return [text]
        parts = [x.strip() for x in self._split_re.split(text) if x.strip()]
        if not parts:
            return [text]
        trailing = self.config.get("remove_trailing_punctuation", ["。", ".", "！", "!", "？", "?", "；", ";"])
        result = []
        for part in parts:
            for punctuation in trailing:
                punctuation = str(punctuation)
                if punctuation and part.endswith(punctuation):
                    part = part[:-len(punctuation)].strip()
                    break
            if part:
                result.append(part)
        return result or [text]

    def _calc_delay(self, text: str) -> float:
        per = float(self.config.get("per_char_delay", 0.4) or 0.4)
        delay = sum(per if "\u4e00" <= char <= "\u9fff" else per / 2 for char in text)
        try:
            lo, hi = [float(x) for x in str(self.config.get("delay_range", "1~16")).replace("-", "~").split("~", 1)]
        except Exception:
            lo, hi = 1.0, 16.0
        return max(lo, min(hi, delay))

    @staticmethod
    def _fingerprint(payload: dict, expression: str, run_once: bool) -> str:
        data = {"target_type": payload["target_type"], "target_id": payload["target_id"], "content": payload["content"], "expression": expression, "run_once": run_once}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _platform_id(self, event) -> str:
        if event is None:
            return str(self.config.get("default_platform", "aiocqhttp"))
        try:
            return str(event.get_platform_id())
        except Exception:
            return str(self.config.get("default_platform", "aiocqhttp"))

    def _cron_expression(self, hour: int, minute: int, day_of_week: int) -> str:
        if day_of_week < 0:
            return f"{minute} {hour} * * *"
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        if day_of_week > 6:
            raise ValueError("day_of_week must be -1 or 0..6")
        return f"{minute} {hour} * * {names[day_of_week]}"

    def _session(self, payload: dict) -> MessageSession:
        kind = MessageType.GROUP_MESSAGE if payload["target_type"] == "group" else MessageType.FRIEND_MESSAGE
        return MessageSession(str(payload.get("platform_name") or "aiocqhttp"), kind, str(payload["target_id"]))

    async def _run_cron_job(self, **payload):
        content = self._render(payload.get("content", ""))
        parts = self._split_text(content)
        session = self._session(payload)
        for index, part in enumerate(parts):
            await self.context.send_message(session, MessageChain([Plain(part)]))
            if index < len(parts) - 1:
                await asyncio.sleep(self._calc_delay(part))
        logger.info("relay: Cron sent to %s %s", payload.get("target_type"), payload.get("target_id"))

    async def _create_cron(self, event, target_id: str, target_type: str, content: str, hour: int, minute: int, day_of_week: int = -1, run_once: bool = False):
        cron = getattr(self.context, "cron_manager", None)
        if cron is None:
            return "创建失败：AstrBot Cron 管理器不可用。"
        target = self._resolve_target(target_id)
        if not target.isdigit():
            return f"❌ 无法解析目标 '{target_id}'。"
        if target_type not in ("private", "group"):
            return "创建失败：target_type 必须是 private 或 group。"
        try:
            hour, minute, day_of_week = int(hour), int(minute), int(day_of_week)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59 or day_of_week < -1 or day_of_week > 6:
                raise ValueError
            expression = self._cron_expression(hour, minute, day_of_week)
        except Exception:
            return "创建失败：时间参数无效，hour 为 0-23、minute 为 0-59、day_of_week 为 -1 或 0-6。"
        payload = {
            "relay_plugin": PLUGIN_NAME,
            "target_type": target_type,
            "target_id": target,
            "content": str(content),
            "platform_name": self._platform_id(event),
            "source_sender": str(event.get_sender_id()) if event is not None else "webui",
        }
        payload["fingerprint"] = self._fingerprint(payload, expression, run_once)
        async with self._lock:
            jobs = await cron.list_jobs("basic")
            for job in jobs:
                old = job.payload if isinstance(job.payload, dict) else {}
                if job.enabled and old.get("relay_plugin") == "astrbot_plugin_relay" and old.get("fingerprint") == payload["fingerprint"]:
                    return f"检测到相同任务已存在，未重复创建。任务ID：{job.job_id}"
            try:
                job = await cron.add_basic_job(name=f"传话 {target_type} {target} {hour:02d}:{minute:02d}", cron_expression=expression, handler=self._run_cron_job, description=f"传话：{str(content)[:100]}", timezone="Asia/Shanghai", payload=payload, persistent=True)
            except (CronJobSchedulingError, ValueError) as exc:
                return f"创建失败：{exc}"
        return f"已创建 AstrBot 持久化任务：{job.job_id}。规则：{expression}，目标：{target_type} {target}。现在可在 AstrBot Cron/任务页面查看、启停和删除。"

    async def _send_split(self, event, target_type: str, target: int, content: str, recall_seconds: int = 0):
        target_event = copy.copy(event)
        target_event.message_obj = copy.copy(event.message_obj)
        if hasattr(event.message_obj, "sender"):
            target_event.message_obj.sender = copy.copy(event.message_obj.sender)
        if target_type == "group":
            target_event.message_obj.group_id = str(target)
        else:
            target_event.message_obj.group_id = None
            target_event.message_obj.sender.user_id = str(target)
        batch = f"{target_type}:{target}:{datetime.now().timestamp()}"
        for index, part in enumerate(self._split_text(self._render(content))):
            result = await target_event.send(MessageChain([Plain(part)]))
            message_id = self._extract_message_id(result)
            if message_id:
                self.sent_messages.append({"message_id": message_id, "batch_id": batch, "target_type": target_type, "target_id": str(target)})
                self.sent_messages = self.sent_messages[-100:]
                if recall_seconds > 0:
                    task = asyncio.create_task(self._recall_later(target_event, message_id, recall_seconds)); self._track(task)
            if index < len(self._split_text(self._render(content))) - 1:
                await asyncio.sleep(self._calc_delay(part))

    async def _recall_later(self, event, message_id, seconds):
        await asyncio.sleep(seconds)
        bot = getattr(event, "bot", None)
        if bot and hasattr(bot, "call_api"):
            try:
                await bot.call_api("delete_msg", message_id=message_id)
            except Exception:
                logger.exception("relay: recall failed")

    @staticmethod
    def _extract_message_id(value: Any):
        if isinstance(value, (str, int)): return str(value)
        if isinstance(value, dict):
            for key in ("message_id", "messageId", "id"):
                if value.get(key) is not None: return str(value[key])
            for key in ("data", "result", "message"):
                found = RelayPlugin._extract_message_id(value.get(key))
                if found: return found
        return None

    @filter.llm_tool(name="relay_message")
    async def relay_message(self, event: AstrMessageEvent, target_id: str, target_type: str, rewritten_content: str, delay_seconds: int = 0, recall_seconds: int = 0):
        """立即或延迟向 QQ 好友或群发送传话消息。

        Args:
            target_id(string): 接收者 QQ 号、群号或已配置别名；多个目标用英文逗号分隔。
            target_type(string): 发送目标类型，只能是 private（私聊）或 group（群聊）。
            rewritten_content(string): 最终发送给对方的正文。
            delay_seconds(number): 延迟发送秒数；立即发送填写 0。
            recall_seconds(number): 发送后自动撤回的秒数；不撤回填写 0。
        """
        if not self._allowed(event): return self._denied()
        results = []
        for item in target_id.replace("，", ",").split(","):
            target = self._resolve_target(item.strip())
            if not target.isdigit(): results.append(f"❌ 无法解析目标 '{item.strip()}'。"); continue
            task = asyncio.create_task(self._delayed_or_send(event, target_type, int(target), rewritten_content, int(delay_seconds), int(recall_seconds))); self._track(task)
            results.append(f"已安排向 {target_type} {target} 发送传话。")
        return "\n".join(results)

    async def _delayed_or_send(self, event, target_type, target, content, delay, recall):
        if delay > 0: await asyncio.sleep(delay)
        await self._send_split(event, target_type, target, content, recall)

    @filter.llm_tool(name="set_periodic_relay")
    async def set_periodic_relay(
        self,
        event: AstrMessageEvent,
        target_id: str,
        target_type: str,
        content: str,
        interval_seconds: int = 0,
        daily_hour: int = -1,
        daily_minute: int = -1,
        day_of_week: int = -1,
    ):
        """创建固定文本的每日或每周 Basic Cron 传话任务。

        Args:
            target_id(string): 接收者 QQ 号、群号或已配置别名；多个目标用英文逗号分隔。
            target_type(string): 发送目标类型，只能是 private（私聊）或 group（群聊）。
            content(string): 到时间后直接发送的固定正文，不要写工具说明或分析。
            interval_seconds(number): 保留参数。当前为保证 AstrBot 页面可管理，必须填写 0，不创建秒级循环任务。
            daily_hour(number): 每日或每周发送小时，范围 0-23；使用周期时间时必须与 daily_minute 同时填写。
            daily_minute(number): 每日或每周发送分钟，范围 0-59；使用周期时间时必须与 daily_hour 同时填写。
            day_of_week(number): 每周发送的星期，0 表示周一到 6 表示周日；每天发送填写 -1。
        """
        if not self._allowed(event): return self._denied()
        content = str(content).strip()
        if not content:
            return "创建失败：缺少要发送的固定文本 content。"
        if daily_hour < 0 and daily_minute < 0 and day_of_week < 0:
            return "创建失败：请提供 daily_hour 和 daily_minute。"
        if daily_hour < 0 or daily_minute < 0:
            return "创建失败：daily_hour 和 daily_minute 必须同时提供。"
        return await self._create_cron(event, target_id, target_type, content, daily_hour, daily_minute, day_of_week)

    @filter.llm_tool(name="list_pending_relays")
    async def list_pending_relays(self, event: AstrMessageEvent):
        """查看当前由传话插件创建的所有待发送和周期性任务。"""
        if not self._allowed(event): return self._denied()
        cron = getattr(self.context, "cron_manager", None)
        if cron is None: return "AstrBot Cron 管理器不可用。"
        lines = []
        for job in await cron.list_jobs("basic"):
            payload = job.payload if isinstance(job.payload, dict) else {}
            if payload.get("relay_plugin") != "astrbot_plugin_relay": continue
            next_run = cron.get_next_run_time(job.job_id) or job.next_run_time
            lines.append(f"- ID: {job.job_id} | 状态: {'启用' if job.enabled else '停用'} | 规则: {job.cron_expression} | 目标: {payload.get('target_type')} {payload.get('target_id')} | 下次: {next_run} | 内容: {str(payload.get('content',''))[:40]}")
        return "【AstrBot 传话任务】\n" + "\n".join(lines) if lines else "当前没有已保存的传话 Cron 任务。"

    @filter.llm_tool(name="cancel_relay_task")
    async def cancel_relay_task(self, event: AstrMessageEvent, task_id: str = "", task_type: str = "", cancel_all: bool = False, confirm: bool = False):
        """取消一个传话任务，或在确认后取消全部传话任务。

        Args:
            task_id(string): AstrBot Cron 页面显示的任务 ID；取消全部时可留空。
            task_type(string): 兼容参数，当前可留空。
            cancel_all(boolean): 是否取消全部传话任务，默认 false。
            confirm(boolean): 取消全部时必须填写 true，默认 false。
        """
        if not self._allowed(event): return self._denied()
        cron = getattr(self.context, "cron_manager", None)
        if cron is None: return "AstrBot Cron 管理器不可用。"
        jobs = [j for j in await cron.list_jobs("basic") if isinstance(j.payload, dict) and j.payload.get("relay_plugin") == "astrbot_plugin_relay"]
        if cancel_all:
            if not confirm: return f"即将删除全部 {len(jobs)} 个传话任务，请再次调用并提供 confirm=true。"
            for job in jobs: await cron.delete_job(job.job_id)
            return f"已删除全部传话任务，共 {len(jobs)} 个。"
        job = next((j for j in jobs if j.job_id == str(task_id).strip()), None)
        if not job: return f"未找到传话任务 {task_id}，请先查看任务列表。"
        await cron.delete_job(job.job_id)
        return f"已删除传话任务 {job.job_id}。"

    @filter.llm_tool(name="recall_last_message")
    async def recall_last_message(self, event: AstrMessageEvent):
        """撤回最近一批由传话插件发送的消息。"""
        if not self._allowed(event): return self._denied()
        if not self.sent_messages: return "记录中没有找到通过传话插件发出的消息。"
        bot = getattr(event, "bot", None)
        if not bot or not hasattr(bot, "call_api"): return "当前 Bot 接口不支持撤回。"
        batch = self.sent_messages[-1].get("batch_id")
        items = [x for x in self.sent_messages if x.get("batch_id") == batch]
        success = 0
        for item in reversed(items):
            try: await bot.call_api("delete_msg", message_id=item["message_id"]); success += 1
            except Exception: logger.exception("relay: recall failed")
        self.sent_messages = [x for x in self.sent_messages if x.get("batch_id") != batch]
        return f"已尝试撤回最近一批消息，成功 {success} 条。"
