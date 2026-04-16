"""Chat CLI command for interactive sessions."""

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from core.agent import Agent
from core.agent_loader import AgentLoader
from utils.config import Config


class ChatLoop:
    """Interactive chat session."""

    def __init__(self, config: Config, agent_id: str | None = None):
        self.config = config
        self.console = Console()

        # Load agent
        loader = AgentLoader(config)
        agent_id = agent_id or config.default_agent
        self.agent_def = loader.load(agent_id)

        # Create agent and session
        self.agent = Agent(self.agent_def, config)
        self.session = self.agent.new_session()

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

        try:
            while True:
                user_input = await asyncio.to_thread(self.get_user_input)

                if user_input.lower() in ('exit', 'quit', 'q'):
                    self.console.print("Goodbye!", style="bold yellow")
                    break

                if not user_input:
                    continue

                try:
                    # command
                    cmd_response = await self.session.command_registry.dispatch(
                        user_input, self.session
                    )
                    if cmd_response is not None:
                        self.console.print(cmd_response)
                        continue

                    # Normal chat
                    response = await self.session.chat(user_input)
                    self.display_agent_response(response)
                except Exception as e:
                    self.console.print(f"Error: {e}", style="bold red")
        
        except (KeyboardInterrupt, EOFError):
            self.console.print("\nGoodbye!", style="bold yellow")


def chat_command(ctx: typer.Context, agent_id: str | None = None) -> None:
    """Start interactive chat session."""
    config = ctx.obj.get("config")

    chat_loop = ChatLoop(config, agent_id=agent_id)
    asyncio.run(chat_loop.run())