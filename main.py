import random
import asyncio
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter

@register("relay_system", "User", "多功能传话插件(支持群组与定时)", "2.0.0")
class RelayPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

    async def _delayed_send(self, bot, target_type: str, target: int, content: str, delay: int):
        """挂载在后台的非阻塞发送任务"""
        await asyncio.sleep(delay)
        try:
            if target_type == "group" and hasattr(bot, 'send_group_msg'):
                await bot.send_group_msg(group_id=target, message=content)
            elif target_type == "private" and hasattr(bot, 'send_private_msg'):
                await bot.send_private_msg(user_id=target, message=content)
        except Exception as e:
            self.context.logger.error(f"定时传话失败: {e}")

    @filter.llm_tool(name="relay_message")
    async def relay_message(
        self, 
        event: AstrMessageEvent, 
        target_id: str, 
        target_type: str,
        rewritten_content: str, 
        delay_seconds: int = 0
    ):
        '''给指定的 QQ 传话，支持私聊、群聊，以及延时发送。
        
        Args:
            target_id (str): 接收者的 QQ 号或群号。
            target_type (str): 目标类型。如果是群聊则填 "group"，私聊则填 "private"。
            rewritten_content (str): 你重构润色后的传话最终内容（需根据用户要求的身份进行改写）。
            delay_seconds (int): 延迟发送的秒数。如果是立即发送则填 0。
        '''
        bot = event.bot
        target = int(target_id)
        
        # 1. 立即发送逻辑
        if delay_seconds <= 0:
            try:
                if target_type == "group" and hasattr(bot, 'send_group_msg'):
                    await bot.send_group_msg(group_id=target, message=rewritten_content)
                elif target_type == "private" and hasattr(bot, 'send_private_msg'):
                    await bot.send_private_msg(user_id=target, message=rewritten_content)
                return f"已立即将消息发送至 {target_type} {target_id}。"
            except Exception as e:
                return f"发送失败：{e}"

        # 2. 定时发送逻辑（带随机波动）
        offset_limit = self.config.get("random_offset_seconds", 300) if isinstance(self.config, dict) else 300
        fluctuation = random.randint(-offset_limit, offset_limit) if offset_limit > 0 else 0
        final_delay = max(0, delay_seconds + fluctuation)

        # 3. 创建后台异步任务
        asyncio.create_task(self._delayed_send(bot, target_type, target, rewritten_content, final_delay))
        
        return f"已成功将任务加入日程。将向 {target_type} {target_id} 延时发送，实际将在约 {final_delay} 秒后执行。"