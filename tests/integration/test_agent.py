
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent

APP_NAME = "test"
USER_ID = "test_user"


def test_agent_live_stream() -> None:
    """The live agent answers over run_live and returns audio plus a transcript."""

    async def _turn() -> tuple[str, int]:
        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID
        )
        runner = Runner(
            agent=root_agent, session_service=session_service, app_name=APP_NAME
        )
        queue = LiveRequestQueue()
        queue.send_content(
            types.Content(role="user", parts=[types.Part(text="What coding tasks are currently running?")])
        )

        transcript, audio_chunks = "", 0
        try:
            async for event in runner.run_live(
                user_id=USER_ID,
                session_id=session.id,
                live_request_queue=queue,
                run_config=RunConfig(response_modalities=["AUDIO"]),
            ):
                if (
                    event.output_transcription
                    and event.output_transcription.finished
                    and event.output_transcription.text
                ):
                    transcript += event.output_transcription.text
                if event.content and event.content.parts:
                    audio_chunks += sum(
                        1 for part in event.content.parts if part.inline_data
                    )
                if event.turn_complete:
                    break
        finally:
            queue.close()
        return transcript, audio_chunks

    # Note: Requires valid live model credentials to execute live turns against the API.
