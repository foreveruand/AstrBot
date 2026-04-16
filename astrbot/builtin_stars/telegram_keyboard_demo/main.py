from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.core.platform.sources.telegram.tg_event import TelegramCallbackQueryEvent


class Main(star.Star):
    """Telegram keyboard demo plugin - demonstrates inline keyboard and callback query handling."""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.click_counts: dict[str, int] = {}  # Store click counts by user_id

    @filter.command("keyboard")
    async def keyboard(self, event: AstrMessageEvent) -> None:
        """Display a message with an inline keyboard button."""
        # Get the original message
        original_msg = event.message_str

        # Create a message with inline keyboard
        result = MessageEventResult()
        result.message(f"You said: {original_msg}")
        result.inline_keyboard(
            [[{"text": "Click me", "callback_data": f"click_{event.get_sender_id()}"}]]
        )
        event.set_result(result)

    @filter.callback_query()
    async def handle_callback(self, event: TelegramCallbackQueryEvent) -> None:
        """Handle button click callbacks."""
        # Parse callback data
        if event.data.startswith("click_"):
            user_id = event.data[6:]  # Extract user_id

            # Increment click count
            self.click_counts[user_id] = self.click_counts.get(user_id, 0) + 1
            count = self.click_counts[user_id]

            # Answer the callback query (shows a toast notification)
            await event.answer_callback_query(text=f"Button clicked {count} times!")

            # Edit the message with new count
            result = MessageEventResult()
            result.message(f"Button clicked {count} times!")
            result.inline_keyboard(
                [[{"text": f"Click me ({count})", "callback_data": f"click_{user_id}"}]]
            )
            event.set_result(result)
