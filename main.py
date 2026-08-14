import random
import asyncio
import copy
import re
from datetime import datetime, timedelta
from collections.abc import Iterator
from dataclasses import dataclass, field
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.message_components import BaseMessageComponent, Plain

@dataclass
class Segment:
    components: list[BaseMessageComponent] = field(default_factory=list)

    def append(self, comp: BaseMessageComponent):
        self.components.append(comp)

    def extend(self, comps: list[BaseMessageComponent]):
        self.components.extend(comps)

    @property
    def text(self) -> str:
        return "".join(c.text for c in self.components if isinstance(c, Plain))

    @property
    def has_media(self) -> bool:
        return any(not isinstance(c, Plain) for c in self.components)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.has_media

    def strip_plain(self):
        for c in self.components:
            if isinstance(c, Plain):
                c.text = c.text.strip()

class Token:
    def __init__(self, text: str, is_split: bool, priority: int = 10**9):
        self.text = text
        self.is_split = is_split
        self.priority = priority

class TextTokenizer:
    KAOMOJI_PATTERN = re.compile(
        r"("
        r"[(\[\uFF08\u3010<]"
        r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
        r"[^\u4e00-\u9fffA-Za-z0-9\s]"
        r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
        r"[)\]\uFF09\u3011>]"
        r")"
        r"|"
        r"([\u25b3\u25a6\u30fb\u30ef\^><\u2267\u2665\uff5e\uff40\u7c32\u2764]{2,15})"
    )

    def __init__(self, pattern: re.Pattern[str], split_priority: dict[str, int]):
        self.pattern = pattern
        self.split_priority = split_priority
        self.quote_chars = {'"', "'", "`"}
        self.pair_map = {
            "“": "”", "《": "》", "（": "）", "(": ")",
            "[": "]", "{": "}", "‘": "’", "【": "】", "<": ">", "「": "」",
        }

    def _protect_kaomoji(self, text: str):
        protected = text
        mapping: dict[str, str] = {}
        for idx, match in enumerate(self.KAOMOJI_PATTERN.findall(text)):
            kaomoji = match[0] or match[1]
            if not kaomoji:
                continue
            placeholder = f"\u200bKAOMOJI_{idx}\u200b"
            protected = protected.replace(kaomoji, placeholder, 1)
            mapping[placeholder] = kaomoji
        return protected, mapping

    def _restore_kaomoji(self, text: str, mapping: dict[str, str]):
        for placeholder, kaomoji in mapping.items():
            text = text.replace(placeholder, kaomoji)
        return text

    def _get_split_priority(self, text: str) -> int:
        priority = 10**9
        for ch in text:
            priority = min(priority, self.split_priority.get(ch, 10**9))
        return priority

    def tokenize(self, text: str) -> Iterator[Token]:
        text, mapping = self._protect_kaomoji(text)
        stack: list[str] = []
        buf = ""
        i = 0

        while i < len(text):
            ch = text[i]
            is_opener = ch in self.pair_map

            if ch == " ":
                prev = text[i - 1] if i > 0 else ""
                next_ = text[i + 1] if i + 1 < len(text) else ""
                if prev.isalnum() and next_.isalnum():
                    buf += ch
                    i += 1
                    continue

            if ch in self.quote_chars:
                if stack and stack[-1] == ch:
                    stack.pop()
                else:
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            if stack:
                expected = self.pair_map.get(stack[-1])
                if ch == expected:
                    stack.pop()
                elif is_opener:
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            if is_opener:
                stack.append(ch)
                buf += ch
                i += 1
                continue

            m = self.pattern.match(text, i)
            if m:
                seg = m.group()
                if seg.strip() == "":
                    buf += seg
                    i += len(seg)
                    continue
                buf += seg
                yield Token(
                    self._restore_kaomoji(buf, mapping),
                    True,
                    self._get_split_priority(seg),
                )
                buf = ""
                i += len(seg)
                continue

            buf += ch
            i += 1

        if buf:
            yield Token(self._restore_kaomoji(buf, mapping), False)

class SegmentBuilder:
    def __init__(self):
        self.segments: list[Segment] = []
        self.current = Segment()

    def append(self, comps):
        self.current.extend(comps)

    def flush(self):
        if self.current.components:
            self.segments.append(self.current)
        self.current = Segment()

    def finalize(self):
        self.flush()
        return self.segments


@register("relay_system", "User", "多功能传话插件(支持群组、定时、批量、动态渲染与白名单)", "4.0.0")
class RelayPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.pending_tasks = {}
        self.periodic_tasks = {}
        self.sent_messages = []
        self._task_counter = 0
        self._update_tokenizer()

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查用户是否在白名单中。若白名单为空，则默认允许。"""
        raw_whitelist = self.config.get("whitelist", [])
        if isinstance(raw_whitelist, str):
            whitelist = [x.strip() for x in raw_whitelist.replace("，", ",").split(",") if x.strip()]
        else:
            whitelist = [str(x).strip() for x in raw_whitelist if str(x).strip()]
            
        if not whitelist:
            return True
            
        sender_id = getattr(event.message_obj.sender, "user_id", None) if event.message_obj and hasattr(event.message_obj, 'sender') else None
        if sender_id and str(sender_id) in whitelist:
            return True
            
        return False

    def _get_denied_msg(self) -> str:
        """获取自定义的权限拒绝提示词，若为空则静默。"""
        msg = self.config.get("permission_denied_msg", "⚠️ 权限拒绝：您不在允许使用传话功能的白名单中。")
        return msg if str(msg).strip() else ""

    def _resolve_target(self, target_id: str) -> str:
        """解析别名。兼容 dict 或类似于 'id=name' 的字符串列表。"""
        aliases_cfg = self.config.get("aliases", [])
        resolved_aliases = {}
        
        if isinstance(aliases_cfg, dict):
            for k, v in aliases_cfg.items():
                resolved_aliases[str(k).strip()] = str(v).strip()
                resolved_aliases[str(v).strip()] = str(k).strip()
        elif isinstance(aliases_cfg, list):
            for item in aliases_cfg:
                item_str = str(item)
                if "=" in item_str:
                    left, right = item_str.split("=", 1)
                    left_id = left.split(":")[-1].strip()
                    name = right.strip()
                    resolved_aliases[name] = left_id
                    
        return resolved_aliases.get(target_id, target_id)

    def _update_tokenizer(self):
        triggers = self.config.get("split_triggers", [",", "，", "。", "！", "？", "；", "\n", ".", "!", "?", ";"])
        parts = []
        has_newline = False
        
        for t in triggers:
            if t in ("\\n", "\n"):
                has_newline = True
            else:
                parts.append(re.escape(t))
                
        if parts:
            escaped_set = "|".join(parts)
            pattern_str = r"([" + escaped_set + r"]" + (r"|\n" if has_newline else "") + r")+"
        else:
            pattern_str = r"(\n)+" if has_newline else r"([。！？；.,!?;])+"
            
        self.split_re = re.compile(pattern_str)
        self.split_priority = {"\n": 1, "。": 2, ".": 2, "！": 3, "!": 3, "？": 3, "?": 3, "；": 4, ";": 4, "，": 5, ",": 5}
        self.tokenizer = TextTokenizer(self.split_re, self.split_priority)

    def _calc_delay(self, text: str) -> float:
        if not text:
            return 0.5
        per_char = self.config.get("per_char_delay", 0.4)
        cn = per_char
        en = cn / 2
        delay = sum(cn if "\u4e00" <= c <= "\u9fff" else en for c in text)
        
        range_str = self.config.get("delay_range", "1~16")
        try:
            parts = range_str.replace("~", "-").split("-")
            min_d = float(parts[0].strip())
            max_d = float(parts[1].strip())
        except Exception:
            min_d, max_d = 1.0, 16.0
            
        return max(min_d, min(max_d, delay))

    def _select_split_points(self, tokens: list[Token], max_count: int) -> set[int]:
        split_idx = [i for i, t in enumerate(tokens) if t.is_split and t.text.strip()]
        if not split_idx:
            return set()
        if max_count <= 0 or len(split_idx) <= max_count - 1:
            return set(split_idx)

        lengths = [len(t.text) for t in tokens]
        total = sum(lengths)
        targets = [total * i / max_count for i in range(1, max_count)]

        split_points: list[tuple[int, int, int]] = []
        acc = 0
        for i, le in enumerate(lengths):
            acc += le
            if tokens[i].is_split:
                split_points.append((i, acc, tokens[i].priority))

        selected: set[int] = set()
        cursor = 0
        window: list[tuple[int, int, int]] = []

        for target in targets:
            while cursor < len(split_points) and split_points[cursor][1] < target:
                window.append(split_points[cursor])
                cursor += 1
            if cursor < len(split_points):
                window.append(split_points[cursor])
                cursor += 1
            if not window:
                break

            best = min(window, key=lambda item: (item[2], abs(item[1] - target), item[0]))
            selected.add(best[0])
            window = [item for item in window if item[0] > best[0]]

        return selected

    def _render_dynamic_content(self, text: str) -> str:
        """在最终发送时动态替换占位符"""
        now = datetime.now()
        rendered = text.replace("{time}", now.strftime("%H:%M:%S"))
        rendered = rendered.replace("{date}", now.strftime("%Y-%m-%d"))
        rendered = rendered.replace("{datetime}", now.strftime("%Y-%m-%d %H:%M:%S"))
        return rendered

    def _split_text(self, text: str) -> list[str]:
        self._update_tokenizer()
        enable_split = self.config.get("enable_smart_split", True) if isinstance(self.config, dict) else True
        if not enable_split or not text:
            return [text]

        max_len = self.config.get("max_text_length", 150)
        if max_len > 0 and len(text) > max_len:
            return [text]

        max_count = self.config.get("max_split_count", 5) if isinstance(self.config, dict) else 5
        tokens = list(self.tokenizer.tokenize(text))
        selected = self._select_split_points(tokens, max_count)
        
        builder = SegmentBuilder()
        for i, token in enumerate(tokens):
            builder.append([Plain(token.text)])
            if i in selected:
                builder.flush()
        segments = builder.finalize()
        
        trailing_puncts = self.config.get("remove_trailing_punctuation", ["。", ".", "！", "!", "？", "?", "；", ";"])
        
        result = []
        for seg in segments:
            seg.strip_plain()
            t = seg.text
            if t:
                for p in trailing_puncts:
                    if t.endswith(p):
                        t = t[:-len(p)].strip()
                        break
                if t:
                    result.append(t)
        return result if result else [text]

    def _prepare_target_event(self, event: AstrMessageEvent, target_type: str, target: int) -> AstrMessageEvent:
        target_event = copy.copy(event)
        target_event.message_obj = copy.copy(event.message_obj)
        if hasattr(event.message_obj, 'sender'):
            target_event.message_obj.sender = copy.copy(event.message_obj.sender)

        if target_type == "group":
            target_event.message_obj.group_id = str(target)
        elif target_type == "private":
            target_event.message_obj.group_id = None
            if hasattr(target_event.message_obj, 'sender'):
                target_event.message_obj.sender.user_id = str(target)
        return target_event

    async def _send_split_messages(self, target_event: AstrMessageEvent, target_type: str, target: int, content: str, recall_after: int = 0):
        # 执行动态渲染
        rendered_content = self._render_dynamic_content(content)
        text_parts = self._split_text(rendered_content)
        
        for i, part in enumerate(text_parts):
            if not part.strip():
                continue
            
            resp = await target_event.send(MessageChain([Plain(part)]))
            
            if resp:
                msg_id = getattr(resp, "message_id", None) or getattr(resp, "id", None)
                if msg_id:
                    self.sent_messages.append({
                        "message_id": msg_id,
                        "target_type": target_type,
                        "target_id": str(target),
                        "content": part,
                        "time": datetime.now().strftime("%H:%M:%S")
                    })
                    if len(self.sent_messages) > 30:
                        self.sent_messages.pop(0)

            if recall_after > 0 and resp:
                asyncio.create_task(self._recall_message_later(target_event, resp, recall_after))

            if i < len(text_parts) - 1:
                delay = self._calc_delay(part)
                await asyncio.sleep(delay)

    async def _recall_message_later(self, event: AstrMessageEvent, send_result, delay: float):
        await asyncio.sleep(delay)
        try:
            bot = getattr(event, "bot", None)
            message_id = getattr(send_result, "message_id", None) or getattr(send_result, "id", None)
            if bot and message_id and hasattr(bot, "call_api"):
                await bot.call_api("delete_msg", message_id=message_id)
        except Exception as e:
            self.context.logger.error(f"消息撤回失败: {e}")

    async def _delayed_send_wrapper(self, task_id: int, event: AstrMessageEvent, target_type: str, target: int, content: str, delay: int):
        try:
            await asyncio.sleep(delay)
            target_event = self._prepare_target_event(event, target_type, target)
            await self._send_split_messages(target_event, target_type, target, content)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.context.logger.error(f"定时传话发送失败: {e}")
        finally:
            self.pending_tasks.pop(task_id, None)

    async def _periodic_send_wrapper(self, task_id: int, event: AstrMessageEvent, target_type: str, target: int, content: str, interval_seconds: int, hour: int = -1, minute: int = -1, day_of_week: int = -1):
        try:
            if day_of_week >= 0 and hour >= 0 and minute >= 0:
                while True:
                    now = datetime.now()
                    target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    days_ahead = day_of_week - target_time.weekday()
                    if days_ahead < 0 or (days_ahead == 0 and target_time <= now):
                        days_ahead += 7
                    target_time += timedelta(days=days_ahead)
                    wait_seconds = (target_time - now).total_seconds()
                    await asyncio.sleep(wait_seconds)
                    
                    target_event = self._prepare_target_event(event, target_type, target)
                    await self._send_split_messages(target_event, target_type, target, content)
                    await asyncio.sleep(60)

            elif hour >= 0 and minute >= 0:
                while True:
                    now = datetime.now()
                    target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target_time <= now:
                        target_time += timedelta(days=1)
                    wait_seconds = (target_time - now).total_seconds()
                    
                    await asyncio.sleep(wait_seconds)
                    target_event = self._prepare_target_event(event, target_type, target)
                    await self._send_split_messages(target_event, target_type, target, content)
                    await asyncio.sleep(60)

            else:
                while True:
                    await asyncio.sleep(interval_seconds)
                    target_event = self._prepare_target_event(event, target_type, target)
                    await self._send_split_messages(target_event, target_type, target, content)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.context.logger.error(f"周期任务执行异常: {e}")
        finally:
            self.periodic_tasks.pop(task_id, None)

    @filter.llm_tool(name="relay_message")
    async def relay_message(
        self, 
        event: AstrMessageEvent, 
        target_id: str, 
        target_type: str,
        rewritten_content: str, 
        delay_seconds: int = 0,
        recall_seconds: int = 0
    ):
        '''给指定的 QQ 传话，支持批量发送。内容会在发送瞬间动态渲染，支持占位符：{time}, {date}, {datetime}。
        
        Args:
            target_id (str): 接收者的 QQ 号、群号或别名。批量发送使用英文逗号分隔（如 "123,456"）。
            target_type (str): "group" 或 "private"。
            rewritten_content (str): 润色后的传话内容，可用 {time} 等占位符来保证定时发出时时间的准确性。
        '''
        if not self._check_permission(event):
            return self._get_denied_msg()
            
        target_ids = [t.strip() for t in target_id.replace("，", ",").split(",") if t.strip()]
        results = []
        
        for single_target in target_ids:
            resolved_target = self._resolve_target(single_target)
            if not resolved_target.isdigit():
                results.append(f"❌ 无法解析目标 '{single_target}'。")
                continue
                
            target = int(resolved_target)
            
            if delay_seconds <= 0:
                try:
                    target_event = self._prepare_target_event(event, target_type, target)
                    asyncio.create_task(self._send_split_messages(target_event, target_type, target, rewritten_content, recall_after=recall_seconds))
                    recall_tip = f"，并将在 {recall_seconds} 秒后自动撤回" if recall_seconds > 0 else ""
                    results.append(f"已立即将分段消息发送至 {target_type} {resolved_target}{recall_tip}。")
                except Exception as e:
                    results.append(f"发送至 {resolved_target} 失败：{e}")
            else:
                offset_limit = self.config.get("random_offset_seconds", 300) if isinstance(self.config, dict) else 300
                fluctuation = random.randint(-offset_limit, offset_limit) if offset_limit > 0 else 0
                final_delay = max(0, delay_seconds + fluctuation)

                self._task_counter += 1
                task_id = self._task_counter
                execute_time = datetime.now() + timedelta(seconds=final_delay)
                
                coro = self._delayed_send_wrapper(task_id, event, target_type, target, rewritten_content, final_delay)
                bg_task = asyncio.create_task(coro)
                
                self.pending_tasks[task_id] = {
                    "task": bg_task,
                    "target_type": target_type,
                    "target_id": resolved_target,
                    "content": rewritten_content,
                    "execute_time": execute_time.strftime("%H:%M:%S")
                }
                
                results.append(f"已成功将任务加入日程（任务ID: P-{task_id}）。将向 {target_type} {resolved_target} 延时发送，预计在 {execute_time.strftime('%H:%M:%S')} 执行（含随机波动）。")
                
        return "\n".join(results)

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
        day_of_week: int = -1
    ):
        '''创建周期性（循环、每日定时或每周特定星期几定时）传话任务，支持批量。内容支持 {time} 等占位符。'''
        if not self._check_permission(event):
            return self._get_denied_msg()
            
        target_ids = [t.strip() for t in target_id.replace("，", ",").split(",") if t.strip()]
        results = []
        
        for single_target in target_ids:
            resolved_target = self._resolve_target(single_target)
            if not resolved_target.isdigit():
                results.append(f"❌ 无法解析目标 '{single_target}'。")
                continue
                
            target = int(resolved_target)
            self._task_counter += 1
            task_id = self._task_counter
            
            coro = self._periodic_send_wrapper(task_id, event, target_type, target, content, interval_seconds, daily_hour, daily_minute, day_of_week)
            bg_task = asyncio.create_task(coro)
            
            week_days_map = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
            desc = ""
            if day_of_week >= 0 and daily_hour >= 0 and daily_minute >= 0:
                w_str = week_days_map.get(day_of_week, "指定星期")
                desc = f"每{w_str} {daily_hour:02d}:{daily_minute:02d}"
            elif daily_hour >= 0 and daily_minute >= 0:
                desc = f"每天固定 {daily_hour:02d}:{daily_minute:02d}"
            elif interval_seconds > 0:
                desc = f"每隔 {interval_seconds} 秒"
            else:
                return "创建失败：请提供正确的周期规则。"

            self.periodic_tasks[task_id] = {
                "task": bg_task,
                "target_type": target_type,
                "target_id": resolved_target,
                "content": content,
                "rule": desc
            }
            
            results.append(f"已成功创建周期性传话任务（周期任务ID: C-{task_id}），规则：{desc}，发送至 {target_type} {resolved_target}。")
            
        return "\n".join(results)

    @filter.llm_tool(name="list_pending_relays")
    async def list_pending_relays(self, event: AstrMessageEvent):
        '''查看当前所有排队中的单次定时任务以及所有周期性循环任务。'''
        if not self._check_permission(event):
            return self._get_denied_msg()
            
        if not self.pending_tasks and not self.periodic_tasks:
            return "当前没有任何排队中的定时任务或周期性任务。"
        
        res = ""
        if self.pending_tasks:
            res += "【单次定时任务】\n"
            for tid, info in self.pending_tasks.items():
                res += f"- 任务ID: P-{tid} | 目标: {info['target_type']} {info['target_id']} | 预计时间: {info['execute_time']} | 内容: {info['content'][:20]}...\n"
        
        if self.periodic_tasks:
            res += "\n【周期性循环任务】\n"
            for tid, info in self.periodic_tasks.items():
                res += f"- 任务ID: C-{tid} | 目标: {info['target_type']} {info['target_id']} | 规则: {info['rule']} | 内容: {info['content'][:20]}...\n"
                
        return res.strip()

    @filter.llm_tool(name="cancel_relay_task")
    async def cancel_relay_task(self, event: AstrMessageEvent, task_id: int = 0, task_type: str = "single", cancel_all: bool = False):
        '''取消指定的单次定时任务或周期性任务。'''
        if not self._check_permission(event):
            return self._get_denied_msg()
            
        if cancel_all:
            p_count = len(self.pending_tasks)
            c_count = len(self.periodic_tasks)
            for info in list(self.pending_tasks.values()):
                info["task"].cancel()
            for info in list(self.periodic_tasks.values()):
                info["task"].cancel()
            self.pending_tasks.clear()
            self.periodic_tasks.clear()
            return f"已成功清空所有任务（共取消 {p_count} 个单次定时任务，{c_count} 个周期性任务）。"

        if task_type == "periodic" or task_id in self.periodic_tasks:
            if task_id in self.periodic_tasks:
                info = self.periodic_tasks.pop(task_id)
                info["task"].cancel()
                return f"已成功取消周期性任务 #C-{task_id}（发往 {info['target_type']} {info['target_id']}）。"
            
        if task_id in self.pending_tasks:
            info = self.pending_tasks.pop(task_id)
            info["task"].cancel()
            return f"已成功取消单次定时任务 #P-{task_id}（发往 {info['target_type']} {info['target_id']}）。"
        
        return f"未找到对应 ID 的任务，请先使用任务列表查看工具确认。"

    @filter.llm_tool(name="recall_last_message")
    async def recall_last_message(self, event: AstrMessageEvent):
        '''手动撤回最近通过插件发出的一条消息。'''
        if not self._check_permission(event):
            return self._get_denied_msg()
            
        if not self.sent_messages:
            return "记录中没有找到通过插件发出的消息可供撤回。"
        
        last_msg = self.sent_messages.pop()
        try:
            bot = getattr(event, "bot", None)
            if bot and hasattr(bot, "call_api"):
                await bot.call_api("delete_msg", message_id=last_msg["message_id"])
                return f"已成功手动撤回发往 {last_msg['target_type']} {last_msg['target_id']} 的消息：「{last_msg['content']}」"
            else:
                return "当前 Bot 接口不支持调用撤回方法。"
        except Exception as e:
            return f"撤回失败（可能已超出 QQ 最大撤回时间或消息已被撤回）：{e}"
