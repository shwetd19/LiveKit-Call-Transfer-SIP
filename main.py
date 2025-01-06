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

        
load_dotenv(dotenv_path=".env.local")

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

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

        # Format the transfer number with SIP URI format
        transfer_uri = f"tel:{transfer_number}"
        
        # Create transfer request with proper SIP REFER format
        transfer_request = proto_sip.TransferSIPParticipantRequest(
            room_name=room_name,
            participant_identity=participant_identity,
            transfer_to=transfer_uri,
            play_dialtone=False
        )

        logger.info(f"Initiating transfer for participant {participant_identity} to {transfer_uri}")
        await livekit_api.sip.transfer_sip_participant(transfer_request)
        
        # Wait briefly to ensure transfer is initiated
        await asyncio.sleep(2)
        
        # Clean up and exit
        await assistant.cleanup()
        
        # Exit the process after transfer
        logger.info("Transfer completed, exiting process")
        os._exit(0)  # Force exit the process after transfer
        
        return True

    except Exception as e:
        logger.error(f"Transfer failed: {e}", exc_info=True)
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

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    print(f"Room name is: {ctx.room.name}") 

    organizationId = ctx.room.name.replace("call-","")
    print(f"organizationId is: {organizationId}")
    context = await fetch_context_from_organization_id(organizationId)
    print(f"Context is: {context}")
    initial_ctx.append(
        role="system", 
        text=(
            context
        ),
    )
    

    # VoiceAssistant is a class that creates a full conversational AI agent.
    assistant = VoiceAssistant(
        vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(),
        chat_ctx=initial_ctx,
    )

    assistant.start(ctx.room)

    await asyncio.sleep(1)

    # Updated greeting to remove DTMF options
    greeting = (
        "Hey, how can I help you today? If you need to speak with our billing department, "
        "just let me know by saying 'yes', and I'll transfer you right away."
    )
    await assistant.say(greeting, allow_interruptions=True)

    # Set up a message handler for the assistant
    @assistant.on_message
    async def handle_message(message: str):
        message = message.lower().strip()
        
        if message in ["yes", "yeah", "sure", "okay", "correct", "yep"]:
            participants = list(ctx.room.participants.values())
            if not participants:
                logger.error("No participants found in room")
                await assistant.say("I'm sorry, but I cannot process the transfer at this moment.", allow_interruptions=True)
                return

            participant = participants[0]
            logger.info(f"Attempting transfer for participant: {participant.identity}")
            await handle_transfer(ctx.room.name, participant.identity, assistant)
            
        elif message in ["no", "nope", "not now", "nah"]:
            await assistant.say("Alright, is there something else I can help you with?", allow_interruptions=True)
        else:
            await assistant.say("Would you like me to transfer you to our billing department? Please say 'yes' or 'no'.", allow_interruptions=True)

    # Keep the agent running
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    # logger.info("Starting LiveKit AI Agent")
    # Initialize the worker with the entrypoint
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))