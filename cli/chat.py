"""Chat CLI command for interactive sessions."""

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from core.agent import Agent
from core.context import SharedContext
from core.events import (
    OutboundEvent,
    InboundEvent,
)
from server import (
    AgentWorker,
    Worker,
)
from utils.config import Config


class ChatLoop:
    """Interactive chat session."""

    def __init__(self, config: Config, agent_id: str | None = None):
        self.config = config
        self.console = Console()
        self.context = SharedContext(config=config)

        self.workers: list[Worker] = [
            self.context.eventbus,
            AgentWorker(self.context)
        ]

        self.response_queue: asyncio.Queue[OutboundEvent] = asyncio.Queue()
        self.context.eventbus.subscribe(OutboundEvent, self.handle_outbound_event)

        agent_id = agent_id or config.default_agent
        self.agent_def = self.context.agent_loader.load(agent_id)

    async def handle_outbound_event(self, event: OutboundEvent) -> None:
        """Handle outbound events by adding to response queue."""
        await self.response_queue.put(event)

    def get_user_input(self) -> str:
        """Get user input with styled prompt."""
        promt_text = Text("You", style="cyan")
        user_input = Prompt.ask(promt_text, console=self.console)
        return user_input.strip()
    
    def display_agent_response(self, content: str) -> None:
        """Display agent response with styled prefix."""
        prefix = Text(f"{self.agent_def.id}: ", style="green")

        self.console.print(prefix, end="")
        self.console.print(content)
    

    async def run(self) -> None:
        self.console.print(
            Panel(
                Text("Welcome to the Bot! Type 'exit' to quit.", style="bold cyan"),
                title="Chat",
                border_style="cyan"
            )
        )
        self.console.print("Type '/help' for commands, 'quit' or 'exit' to end the session.\n")

        for worker in self.workers:
            worker.start()

        session_id = (
            Agent(self.agent_def, self.context).new_session().session_id
        )

        try:
            while True:
                user_input = await asyncio.to_thread(self.get_user_input)

                if user_input.lower() in ('exit', 'quit', 'q'):
                    self.console.print("Goodbye!", style="bold yellow")
                    break

                if not user_input:
                    continue

                event = InboundEvent(
                    session_id=session_id,
                    content=user_input,
                )
                await self.context.eventbus.publish(event)

                try:
                    # Normal chat
                    response = await asyncio.wait_for(
                        self.response_queue.get(), timeout=60.0
                    )
                    self.display_agent_response(response.content)
                except asyncio.TimeoutError:
                    self.console.print("[red]Agent response timed out[/red]")
                    self.console.print()
        
        except (KeyboardInterrupt, EOFError):
            self.console.print("\nGoodbye!", style="bold yellow")
        finally:
            for worker in self.workers:
                await worker.stop()


def chat_command(ctx: typer.Context, agent_id: str | None = None) -> None:
    """Start interactive chat session."""
    config = ctx.obj.get("config")

    chat_loop = ChatLoop(config, agent_id=agent_id)
    asyncio.run(chat_loop.run())