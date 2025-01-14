from __future__ import annotations
import asyncio
import logging
import os
from typing import Annotated, Optional
import aiohttp
from dotenv import load_dotenv
from livekit import rtc, api
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.protocol import sip as proto_sip
from livekit.plugins import deepgram, openai, silero
import re
from pathlib import Path
from datetime import datetime

# Initialize logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("voice-assistant")
logger.setLevel(logging.INFO)

def load_environment():
    """Load environment variables from available .env files"""
    current_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    env_files = [
        current_dir / '.env.local',
        current_dir / '.env',
        Path.home() / 'livekit-agent-inb/.env.local',
        Path.home() / 'livekit-agent-inb/.env'
    ]
    
    env_loaded = False
    for env_file in env_files:
        if env_file.exists():
            try:
                load_dotenv(dotenv_path=str(env_file))
                logger.info(f"Loaded environment from: {env_file}")
                env_loaded = True
                break
            except Exception as e:
                logger.error(f"Error loading {env_file}: {str(e)}")
    
    if not env_loaded:
        logger.error("No environment files could be loaded!")
        return False
    
    required_vars = [
        'LIVEKIT_URL',
        'LIVEKIT_API_KEY',
        'LIVEKIT_API_SECRET',
        'OPENAI_API_KEY',
        'BILLING_PHONE_NUMBER',
        'CAL_API_KEY',
        'CAL_EVENT_TYPE_ID'
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return False
    
    return True

class CalendarFunctions(llm.FunctionContext):
    def __init__(self):
        super().__init__()
        
        # Initialize Cal.com specific attributes
        self.api_key = os.getenv('CAL_API_KEY')
        self.event_type_id = os.getenv('CAL_EVENT_TYPE_ID')
        self.base_url = 'https://api.cal.com/v1'
        
    @llm.ai_callable()
    async def schedule_appointment(
        self,
        date: Annotated[str, llm.TypeInfo(description="The preferred date for the appointment (YYYY-MM-DD)")],
        time: Annotated[str, llm.TypeInfo(description="The preferred time for the appointment (HH:MM)")],
        name: Annotated[str, llm.TypeInfo(description="Customer's full name")],
        email: Annotated[str, llm.TypeInfo(description="Customer's email address")],
        notes: Annotated[Optional[str], llm.TypeInfo(description="Any additional notes for the appointment")] = None
    ) -> str:
        """Schedule an appointment on Cal.com when a user requests to book a meeting."""
        try:
            # Validate date and time format
            try:
                datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            except ValueError as e:
                return f"Invalid date or time format. Please use YYYY-MM-DD for date and HH:MM for time. Error: {str(e)}"

            # Construct the datetime string in ISO format
            # Convert to UTC assuming the input is in local time
            start_time = f"{date}T{time}:00.000Z"
            
            # Prepare the booking payload
            payload = {
                "eventTypeId": int(self.event_type_id),
                "start": start_time,
                "end": None,  # Cal.com will calculate this based on event duration
                "email": email,
                "name": name,
                "notes": notes or "",
                "language": "en",
                "timeZone": "UTC",  # Explicitly set timezone
                "bookingRequest": {
                    "respondOnce": True
                }
            }
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            logger.info(f"Sending booking request to Cal.com: {payload}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/bookings",
                    json=payload,
                    headers=headers
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Cal.com API response: {response.status} - {response_text}")
                    
                    if response.status in [201, 200]:
                        return f"Great! I've successfully scheduled your appointment for {date} at {time}. You'll receive a confirmation email shortly."
                    else:
                        error_msg = f"Failed to schedule appointment. Status: {response.status}. Response: {response_text}"
                        logger.error(error_msg)
                        return "I couldn't schedule the appointment at this time. The time slot might be unavailable. Would you like to try a different time?"
                        
        except aiohttp.ClientError as e:
            error_msg = f"Network error while scheduling appointment: {str(e)}"
            logger.error(error_msg)
            return "I'm having trouble connecting to the scheduling system. Please try again in a moment."
            
        except Exception as e:
            error_msg = f"Unexpected error while scheduling appointment: {str(e)}"
            logger.error(error_msg)
            return "I encountered an unexpected error while scheduling your appointment. Would you like to try again?"

class VoiceTransferAssistant:
    def __init__(self, context: JobContext):
        self.context = context
        self.assistant = None
        self.livekit_api = None
        self.transfer_in_progress = False
        self.calendar_functions = CalendarFunctions()

    async def initialize(self) -> bool:
            """Initialize the assistant and API client"""
            try:
                # Initialize LiveKit API
                livekit_url = os.getenv('LIVEKIT_URL')
                api_key = os.getenv('LIVEKIT_API_KEY')
                api_secret = os.getenv('LIVEKIT_API_SECRET')
                
                logger.debug(f"Initializing LiveKit API client with URL: {livekit_url}")
                self.livekit_api = api.LiveKitAPI(
                    url=livekit_url,
                    api_key=api_key,
                    api_secret=api_secret
                )

                # Create initial chat context with more specific scheduling instructions
                initial_ctx = llm.ChatContext().append(
                    role="system",
                    text=(
                        "You are a voice assistant that helps with scheduling appointments and billing transfers.\n\n"
                        "For scheduling appointments:\n"
                        "1. Ask for and collect ALL required information in this order:\n"
                        "   - Full name\n"
                        "   - Email address\n"
                        "   - Preferred date (in YYYY-MM-DD format)\n"
                        "   - Preferred time (in HH:MM format, 24-hour)\n"
                        "2. Before calling schedule_appointment, confirm all information is correct\n"
                        "3. If scheduling fails, offer to try a different time\n\n"
                        "For billing transfers:\n"
                        "- If a user says 'yes', initiate transfer to billing\n"
                        "- If they say 'no', ask if they'd like to schedule an appointment\n\n"
                        "General guidelines:\n"
                        "- Keep responses concise and natural\n"
                        "- Confirm understanding after each piece of information\n"
                        "- If any error occurs, apologize and offer to try again"
                    )
                )

                # Initialize voice assistant with updated settings
                self.assistant = VoiceAssistant(
                    vad=silero.VAD.load(),
                    stt=deepgram.STT(),
                    llm=openai.LLM(),
                    tts=openai.TTS(),
                    chat_ctx=initial_ctx,
                    fnc_ctx=self.calendar_functions,
                    allow_interruptions=True,
                    interrupt_speech_duration=0.5,
                    min_endpointing_delay=0.5,
                )

                return True
            except Exception as e:
                logger.error(f"Initialization failed: {e}", exc_info=True)
                return False

    async def transfer_call(self, participant_identity: str) -> bool:
        """Transfer the call using tel: format"""
        if self.transfer_in_progress:
            logger.warning("Transfer already in progress")
            return False

        try:
            self.transfer_in_progress = True
            transfer_to = os.getenv('BILLING_PHONE_NUMBER')
            if not transfer_to:
                logger.error("Billing phone number not configured")
                return False

            # Format transfer number
            if not transfer_to.startswith('+'):
                transfer_to = f"+{transfer_to}"

            # Use tel: format for transfer
            transfer_uri = f"tel:{transfer_to}"
            logger.info(f"Transferring call for participant {participant_identity} to {transfer_uri}")

            # Create transfer request
            transfer_request = proto_sip.TransferSIPParticipantRequest(
                participant_identity=participant_identity,
                room_name=self.context.room.name,
                transfer_to=transfer_uri,
                play_dialtone=True
            )
            logger.debug(f"Transfer request: {transfer_request}")

            # Execute transfer
            await self.livekit_api.sip.transfer_sip_participant(transfer_request)
            logger.info(f"Successfully transferred participant {participant_identity}")
            return True

        except Exception as e:
            logger.error(f"Failed to transfer call: {e}", exc_info=True)
            return False
        finally:
            self.transfer_in_progress = False

    def handle_user_speech(self, msg: llm.ChatMessage):
        async def process_speech():
            try:
                # Process the message
                if isinstance(msg.content, list):
                    message = " ".join(str(x) for x in msg.content if not isinstance(x, llm.ChatImage))
                else:
                    message = str(msg.content)
                
                message = re.sub(r'[^a-zA-Z0-9\s]', '', message.lower().strip())
                logger.info(f"Processed voice input: '{message}'")
                
                # Handle positive responses for billing transfer
                if message in ["yes", "yeah", "sure", "okay", "correct", "yep"]:
                    participants = list(self.context.room.remote_participants.values())
                    if not participants:
                        logger.error("No participants found")
                        await self.assistant.say("I cannot process the transfer right now.", allow_interruptions=True)
                        return

                    participant = participants[0]
                    logger.info(f"Starting transfer for participant: {participant.identity}")
                    
                    # Notify user
                    await self.assistant.say("Transferring you to billing. Please hold.", allow_interruptions=False)
                    await asyncio.sleep(1)
                    
                    # Execute transfer
                    transfer_success = await self.transfer_call(participant.identity)
                    
                    if transfer_success:
                        logger.info("Transfer completed successfully")
                        await self.cleanup()
                    else:
                        await self.assistant.say("I couldn't complete the transfer. Please try again.", allow_interruptions=True)
                
                elif message in ["no", "nope", "not now", "nah"]:
                    await self.assistant.say("Would you like to schedule an appointment instead?", allow_interruptions=True)
                else:
                    # Use LLM directly for message processing
                    response = await self.assistant.llm.complete(
                        messages=[{"role": "user", "content": message}],
                        context=self.assistant.chat_ctx
                    )
                    if response and response.content:
                        await self.assistant.say(response.content, allow_interruptions=True)

            except Exception as e:
                logger.error(f"Speech processing error: {str(e)}", exc_info=True)
                await self.assistant.say("I encountered an error. Please try again.", allow_interruptions=True)

        # Create and run the speech processing task
        asyncio.create_task(process_speech())

    async def cleanup(self):
        """Clean up resources"""
        try:
            if self.livekit_api:
                await self.livekit_api.aclose()
                self.livekit_api = None
            if self.assistant:
                # Stop audio processing
                self.assistant.stop_processing()
                # Clean up any other resources
                await self.assistant.tts.aclose()
                await self.assistant.stt.aclose()
                await self.assistant.llm.aclose()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)

    async def fetch_context(self):
        """Fetch organization context"""
        try:
            organization_id = self.context.room.name.replace("call-", "")
            logger.info(f"Fetching context for organization ID: {organization_id}")
            if self.assistant and hasattr(self.assistant, 'chat_ctx'):
                self.assistant.chat_ctx.append(
                    role="system",
                    text=f"Organization ID: {organization_id}"
                )
        except Exception as e:
            logger.error(f"Error fetching context: {e}")

    async def start(self):
        """Start the assistant and set up event handlers"""
        try:
            # Connect to room
            await self.context.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

            # Register event handlers
            if self.assistant:
                self.assistant.on("user_speech_committed", self.handle_user_speech)
                self.assistant.on("user_started_speaking", lambda: logger.info("User started speaking"))
                self.assistant.on("user_stopped_speaking", lambda: logger.info("User stopped speaking"))
                self.assistant.on("agent_speech_interrupted", lambda: logger.info("Agent was interrupted"))

                # Start assistant
                self.assistant.start(self.context.room)
                await asyncio.sleep(1)

                # Fetch context and send greeting
                await self.fetch_context()
                greeting = (
                    "Hello! I can help you schedule an appointment or connect you with billing. "
                    "For billing matters, just say 'yes' and I'll transfer you. "
                    "Would you like to schedule an appointment or speak with billing?"
                )
                await self.assistant.say(greeting, allow_interruptions=True)

        except Exception as e:
            logger.error(f"Error starting assistant: {e}", exc_info=True)

    async def cleanup(self):
        """Clean up resources"""
        try:
            if self.livekit_api:
                await self.livekit_api.aclose()
                self.livekit_api = None
            if self.assistant:
                await self.assistant.stop()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)

async def entrypoint(context: JobContext):
    """Main entry point"""
    # Load environment variables
    if not load_environment():
        logger.error("Failed to load required environment variables")
        return

    assistant = VoiceTransferAssistant(context)
    
    if not await assistant.initialize():
        logger.error("Failed to initialize assistant")
        return

    disconnect_event = asyncio.Event()

    @context.room.on("disconnected")
    def on_room_disconnect(*args):
        disconnect_event.set()

    try:
        await assistant.start()
        await disconnect_event.wait()
    finally:
        await assistant.cleanup()

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))