import asyncio
import logging
import aiohttp
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.plugins import deepgram, openai, silero
from livekit import api, rtc
from livekit.protocol import sip as proto_sip
import os
from dotenv import load_dotenv
from pathlib import Path

def load_environment():
    """
    Load environment variables from available .env files
    Returns True if successful, False if no environment files could be loaded
    """
    # Get the current script's directory
    current_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    
    # Define potential env file locations
    env_files = [
        current_dir / '.env.local',
        current_dir / '.env',
        Path.home() / 'livekit-agent-inb/.env.local',
        Path.home() / 'livekit-agent-inb/.env'
    ]
    
    env_loaded = False
    
    # Try loading each env file in order
    for env_file in env_files:
        if env_file.exists():
            try:
                load_dotenv(dotenv_path=str(env_file))
                logger.info(f"Loaded environment from: {env_file}")
                env_loaded = True
                break
            except Exception as e:
                logger.error(f"Error loading {env_file}: {str(e)}")
                continue
    
    if not env_loaded:
        logger.error("No environment files could be loaded!")
        return False
    
    # Verify critical environment variables
    required_vars = [
        'LIVEKIT_URL',
        'LIVEKIT_API_KEY',
        'LIVEKIT_API_SECRET',
        'OPENAI_API_KEY',
        'BILLING_PHONE_NUMBER'
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return False
    
    logger.info("Environment loaded successfully with all required variables")
    return True

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Replace the simple load_dotenv call with the new function
if not load_environment():
    logger.error("Failed to load required environment variables")
    exit(1)

# Simplified to just one department
TRANSFER_CONFIG = {
    "PHONE_NUMBER": ("BILLING_PHONE_NUMBER", "Billing")
}

async def fetch_context_from_organization_id(organizationId:str) -> str:
    logger.info(f"Fetching context for organization ID: {organizationId}")
    async with aiohttp.ClientSession() as session:
        url = f"https://openmic-webfront-test.vercel.app/api/getContextOfUser?organizationId={organizationId}"
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("context", "Unknown")
            else:
                logger.error(f"Failed to fetch context: {response.status}")
                return "Unknown"
            

async def handle_transfer(room_name: str, participant_identity: str, assistant: VoiceAssistant) -> None:
    """
    Handle the transfer process with voice confirmation and SIP REFER.
    """
    try:
        # Get the transfer number from environment
        transfer_number = os.getenv('BILLING_PHONE_NUMBER')
        if not transfer_number:
            raise ValueError("Billing phone number not configured")

        # Ensure transfer_number starts with + if it's not already formatted
        if not transfer_number.startswith('+'):
            transfer_number = f"+{transfer_number}"

        await assistant.say("Transferring you to our billing department. Please hold.", allow_interruptions=False)
        
        # Initialize LiveKit API client
        livekit_api = api.LiveKitAPI(
            url=os.getenv('LIVEKIT_URL'),
            api_key=os.getenv('LIVEKIT_API_KEY'),
            api_secret=os.getenv('LIVEKIT_API_SECRET')
        )

        # Format the transfer number with SIP URI format and domain
        transfer_uri = f"sip:{transfer_number}@sip.livekit.io"
        
        logger.info(f"Preparing transfer request for {participant_identity} to {transfer_uri}")
        
        # Create transfer request with explicit parameters
        transfer_request = proto_sip.TransferSIPParticipantRequest()
        transfer_request.room_name = room_name
        transfer_request.participant_identity = participant_identity
        transfer_request.transfer_to = transfer_uri

        logger.info(f"Executing transfer request: {transfer_request}")
        
        # Execute transfer and capture response
        response = await livekit_api.sip.transfer_sip_participant(transfer_request)
        logger.info(f"Transfer API response: {response}")
        
        # Wait for transfer to process
        await asyncio.sleep(2)
        
        # Stop the assistant before cleanup
        await assistant.stop()
        await assistant.cleanup()
        
        logger.info("Transfer completed successfully")
        
        # Exit process after successful transfer
        os._exit(0)

    except Exception as e:
        logger.error(f"Transfer failed with exception: {str(e)}", exc_info=True)
        await assistant.say("I apologize, but I couldn't transfer your call. Please try again later.", allow_interruptions=True)
        return False

async def entrypoint(ctx: JobContext):
    # Create an initial chat context with a system prompt
    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are a voice assistant created by Attack Capital. Your interface with users is voice, and you should "
            "respond with short, concise, and natural language. You are polite, helpful, and always ready to assist. "
            "If a user says 'yes', initiate a transfer to the billing department. "
            "If they say 'no', ask if there's anything else you can help with. "
            "At the start of a conversation, introduce yourself and offer your assistance. "
            "Listen carefully for 'yes' or 'no' responses when asking about transfers."
        ),
    )

    # Create VoiceAssistant with explicit API key configuration
    assistant = VoiceAssistant(
        vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(),
        chat_ctx=initial_ctx,
        allow_interruptions=True,
        interrupt_speech_duration=0.5,
        min_endpointing_delay=0.5,
    )

    # Set up event handlers
    def handle_user_speech(msg: llm.ChatMessage):
        async def process_speech():
            try:
                logger.info(f"Processing user speech: {msg.content}")
                
                if isinstance(msg.content, list):
                    message = " ".join(str(x) for x in msg.content if not isinstance(x, llm.ChatImage))
                else:
                    message = str(msg.content)
                
                message = message.lower().strip()
                logger.info(f"Processed message: '{message}'")
                
                if message in ["yes", "yeah", "sure", "okay", "correct", "yep"]:
                    logger.info("Positive response detected, initiating transfer")
                    participants = list(ctx.room.participants.values())
                    
                    if not participants:
                        logger.error("No participants found in room")
                        await assistant.say("I'm sorry, but I cannot process the transfer at this moment.", allow_interruptions=True)
                        return

                    participant = participants[0]
                    logger.info(f"Found participant: {participant.identity}")
                    
                    # Create transfer task
                    transfer_task = asyncio.create_task(
                        handle_transfer(ctx.room.name, participant.identity, assistant)
                    )
                    
                    try:
                        # Wait for transfer with timeout
                        await asyncio.wait_for(transfer_task, timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.error("Transfer operation timed out")
                        await assistant.say("I'm sorry, the transfer is taking longer than expected. Please try again.", allow_interruptions=True)
                    except Exception as e:
                        logger.error(f"Transfer task failed: {str(e)}", exc_info=True)
                        await assistant.say("I encountered an error while trying to transfer your call. Please try again.", allow_interruptions=True)
                
                elif message in ["no", "nope", "not now", "nah"]:
                    await assistant.say("Alright, is there something else I can help you with?", allow_interruptions=True)
                else:
                    await assistant.say("Would you like me to transfer you to our billing department? Please say 'yes' or 'no'.", allow_interruptions=True)
                    
            except Exception as e:
                logger.error(f"Error in process_speech: {str(e)}", exc_info=True)
                await assistant.say("I encountered an unexpected error. Please try again.", allow_interruptions=True)

        # Create and run the async task
        asyncio.create_task(process_speech())

    # Register the synchronous event handlers
    assistant.on("user_speech_committed", handle_user_speech)

    def on_user_started_speaking():
        logger.info("User started speaking")

    def on_user_stopped_speaking():
        logger.info("User stopped speaking")

    def on_agent_interrupted():
        logger.info("Agent was interrupted")

    # Register the debug event handlers
    assistant.on("user_started_speaking", on_user_started_speaking)
    assistant.on("user_stopped_speaking", on_user_stopped_speaking)
    assistant.on("agent_speech_interrupted", on_agent_interrupted)

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    print(f"Room name is: {ctx.room.name}")
    organizationId = ctx.room.name.replace("call-","")
    print(f"organizationId is: {organizationId}")
    context = await fetch_context_from_organization_id(organizationId)
    print(f"Context is: {context}")
    initial_ctx.append(
        role="system", 
        text=context,
    )

    # Start the assistant and send greeting
    assistant.start(ctx.room)
    await asyncio.sleep(1)

    greeting = (
        "Hey, how can I help you today? If you need to speak with our billing department, "
        "just let me know by saying 'yes', and I'll transfer you right away."
    )
    await assistant.say(greeting, allow_interruptions=True)

    # Keep the agent running
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    # logger.info("Starting LiveKit AI Agent")
    # Initialize the worker with the entrypoint
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))