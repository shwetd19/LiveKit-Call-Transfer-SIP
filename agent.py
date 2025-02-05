from __future__ import annotations
import asyncio
import logging
import os
from typing import Annotated, Optional
import aiohttp
import json
from dotenv import load_dotenv
from livekit import rtc, api
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.protocol import sip as proto_sip
from livekit.plugins import deepgram, openai, silero
import re
from pathlib import Path
from datetime import datetime, timedelta

load_dotenv()  # Loads variables from .env file into the environment

# Initialize logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("voice-assistant")
logger.setLevel(logging.INFO)

DEPARTMENT_NUMBERS = {
    "1": ("BILLING_PHONE_NUMBER", "Billing"),
    "2": ("TECH_SUPPORT_PHONE_NUMBER", "Tech Support"),
    "3": ("CUSTOMER_SERVICE_PHONE_NUMBER", "Customer Service")
}

def load_environment() -> bool:
    """Load environment variables from the .env file in the same directory."""
    current_dir = Path(__file__).parent
    env_file = current_dir / ".env"  # Explicitly load from the script's directory

    if env_file.exists():
        load_dotenv(dotenv_path=str(env_file), override=True)
        logger.info(f"Loaded environment from: {env_file}")
    else:
        logger.error("No .env file found in the directory!")
        return False

    required_vars = [
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "OPENAI_API_KEY",
        "BILLING_PHONE_NUMBER",
        #  "CAL_API_KEY",
    #     "CAL_EVENT_TYPE_ID",
    ]

    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
        return False

    return True

class VoiceTransferAssistant:
    def __init__(self, context: JobContext):
        self.context = context
        self.assistant = None
        self.livekit_api = None
        self.transfer_in_progress = False
        self.conversation_state = {
            'current_step': 'initial',
            'collected_info': {
                'name': None,
                'email': None,
                'date': None,
                'time': None
            },
            'appointment_confirmed': False
        }

    async def initialize(self) -> bool:
        """Initialize the assistant and API client"""
        try:
            # Initialize LiveKit API
            livekit_url = os.getenv('LIVEKIT_URL')
            api_key = os.getenv('LIVEKIT_API_KEY')
            api_secret = os.getenv('LIVEKIT_API_SECRET')
            
            self.livekit_api = api.LiveKitAPI(
                url=livekit_url,
                api_key=api_key,
                api_secret=api_secret
            )

            # Initialize VAD with optimized parameters
            vad = silero.VAD.load(
                min_speech_duration=0.1,  # Detect shorter speech
                min_silence_duration=0.1,  # Reduced silence detection duration
                prefix_padding_duration=0.05,  # Reduced padding for faster response
                max_buffered_speech=30.0,
                activation_threshold=0.3  # Lower threshold for detecting the end of speech
            )

            # Initialize voice assistant
            self.assistant = VoiceAssistant(
                vad=vad,
                stt=deepgram.STT(model="nova", interim_results=True),  # Optimized for faster response
                llm=openai.LLM(model="gpt-4o-mini"),
                tts=openai.TTS(voice="fable", speed=1.0),
                chat_ctx=self._create_initial_context(),
                allow_interruptions=True,
                interrupt_speech_duration=0.1,  # Quicker interruption response
                min_endpointing_delay=0.2,  # Reduced to allow faster speech endpoint detection
                max_endpointing_delay=0.5  # Reduced to minimize waiting time for speech end
            )

            self.assistant.on("turn_complete", self._handle_turn_complete_sync)
            self.assistant.on("user_speech_committed", self._handle_user_speech_sync)
            self.assistant.on("user_started_speaking", lambda: logger.info("User started speaking"))
            self.assistant.on("user_stopped_speaking", lambda: logger.info("User stopped speaking"))
            self.assistant.on("agent_speech_interrupted", lambda: logger.info("Agent was interrupted"))

            return True
        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            return False

    def _create_initial_context(self):
        """Create initial chat context with enhanced conversational instructions"""
        return llm.ChatContext().append(role="system", text="")
     # return llm.ChatContext().append(
        #     role="system",
        #     text=(
        #         "You are a voice assistant, that helps with scheduling appointments and billing transfers. "
        #         "Maintain a natural, conversational tone while being efficient.\n\n"
        #         "For scheduling appointments:\n"
        #         "1. Collect information conversationally in this order:\n"
        #         "   - Full name\n"
        #         "   - Email address\n"
        #         "   - Preferred date (in YYYY-MM-DD format)\n"
        #         "   - Preferred time (in HH:MM format, 24-hour)\n"
        #         "2. Allow for natural pauses and thinking time\n"
        #         "3. Handle interruptions gracefully\n"
        #         "4. Confirm information naturally before proceeding\n\n"
        #         "For billing transfers:\n"
        #         "- Listen for both explicit and implicit requests for billing\n"
        #         "- Confirm transfer intention before proceeding\n"
        #         "- Handle transition points smoothly\n\n"

        #         "Conversation guidelines:\n"

        #         "- Your name or <agent_name> is Gemma\n"

        #         "- don't say `how can i assist you today` or similar in the conversation\n"

        #         "- Use a friendly, professional tone\n"
        #         "- Use context-aware responses\n"
        #         "- Allow for natural speech patterns\n"
        #         "- Be patient with pauses and corrections\n"

        #         "- Don't answer anything other than scheduling or billing, if someothe thing is aske just say `sorry i can only help with scheduling call or billing today`"
        #     )
        # )

    def _handle_user_speech_sync(self, msg: llm.ChatMessage):
        asyncio.create_task(self.handle_user_speech(msg))

    def _handle_turn_complete_sync(self, event):
        asyncio.create_task(self._handle_turn_complete_async(event))

    async def _handle_turn_complete_async(self, event):
        """Asynchronous handler for turn completion events"""
        try:
            if event.get('timeout', False):
                await self.assistant.say("I'm listening...", allow_interruptions=True)
            else:
                current_context = self.assistant.chat_ctx.messages[-1].content if self.assistant.chat_ctx.messages else ""
                if "schedule" in str(current_context).lower():
                    if self._needs_more_scheduling_info():
                        await self._request_next_scheduling_info()
                elif "billing" in str(current_context).lower():
                    await self._handle_billing_context()

        except Exception as e:
            logger.error(f"Error handling turn completion: {e}")

    def _needs_more_scheduling_info(self):
        """Check if more scheduling information is needed"""
        required_fields = ['name', 'email', 'date', 'time']
        for msg in reversed(self.assistant.chat_ctx.messages):
            content = str(msg.content).lower()
            for field in required_fields:
                if field in content:
                    required_fields.remove(field)
        return bool(required_fields)

    async def _request_next_scheduling_info(self):
        """Request the next piece of scheduling information"""
        prompts = {
            'name': "Could you tell me your name?",
            'email': "What's the best email to reach you at?",
            'date': "What day works best for you?",
            'time': "What time would you prefer?"
        }

        for field, prompt in prompts.items():
            if not any(field in str(msg.content).lower() for msg in self.assistant.chat_ctx.messages):
                await self.assistant.say(prompt, allow_interruptions=True)
                break

    async def _handle_billing_context(self):
        """Handle billing-related context"""
        try:
            if not self.transfer_in_progress:
                participants = list(self.context.room.remote_participants.values())
                await self.assistant.say("I'll transfer you to the billing department now. One moment please.", allow_interruptions=False)
                if participants:
                    await self.transfer_call(participants[0].identity)
        except Exception as e:
            logger.error(f"Error handling billing context: {e}")
        
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

                # Clean up LiveKit resources
                if self.livekit_api:
                    await self.livekit_api.aclose()
                    self.livekit_api = None

                return True

            except Exception as e:
                logger.error(f"Failed to transfer call: {e}", exc_info=True)
                return False
            finally:
                self.transfer_in_progress = False

    async def handle_user_speech(self, msg: llm.ChatMessage):
        try:
            user_message = ""
            if isinstance(msg.content, list):
                user_message = " ".join(str(x) for x in msg.content if not isinstance(x, llm.ChatImage))
            else:
                user_message = str(msg.content)
            
            user_message = re.sub(r'[^a-zA-Z0-9\s@.]', '', user_message.lower().strip())
            logger.info(f"Processed voice input: '{user_message}'")

            if ("billing" in user_message or 
                (user_message in ["yes", "yeah", "sure", "okay", "correct", "yep", "right"] and 
                any("billing" in str(m.content).lower() for m in self.assistant.chat_ctx.messages[-3:]))):
                
                await self.assistant.say("I'll transfer you to the billing department now. One moment please.", allow_interruptions=False)
                participants = list(self.context.room.remote_participants.values())
                if participants:
                    success = await self.transfer_call(participants[0].identity)
                    if not success:
                        await self.assistant.say("I apologize, but I'm having trouble transferring your call. Please try again later.", allow_interruptions=True)
                return

            if self.conversation_state['appointment_confirmed']:
                if "billing" in user_message:
                    await self.assistant.say("I'll transfer you to billing now.", allow_interruptions=False)
                    participants = list(self.context.room.remote_participants.values())
                    if participants:
                        await self.transfer_call(participants[0].identity)
                elif "thank" in user_message:
                    await self.assistant.say("You're welcome! Have a great day!", allow_interruptions=True)
                return

            if ("schedule" in user_message or "appointment" in user_message) and self.conversation_state['current_step'] == 'initial':
                self.conversation_state['current_step'] = 'name'
                await self.assistant.say("Could you please provide your full name?", allow_interruptions=True)
                return

            if self.conversation_state['current_step'] == 'name' and not '@' in user_message:
                self.conversation_state['collected_info']['name'] = user_message
                self.conversation_state['current_step'] = 'email'
                await self.assistant.say("Thank you. Could you please provide your email address?", allow_interruptions=True)

            elif self.conversation_state['current_step'] == 'email' and '@' in user_message:
                self.conversation_state['collected_info']['email'] = user_message
                self.conversation_state['current_step'] = 'date'
                await self.assistant.say("What date would you prefer for the appointment? Please use YYYY-MM-DD format.", allow_interruptions=True)

            elif self.conversation_state['current_step'] == 'date':
                date_str = self._format_date(user_message)
                if date_str:
                    self.conversation_state['collected_info']['date'] = date_str
                    self.conversation_state['current_step'] = 'time'
                    await self.assistant.say("What time would you prefer? Please use HH:MM format (24-hour).", allow_interruptions=True)
                else:
                    await self.assistant.say("I didn't quite get that date. Could you please provide it in YYYY-MM-DD format?", allow_interruptions=True)

            elif self.conversation_state['current_step'] == 'time':
                time_str = self._format_time(user_message)
                if time_str:
                    self.conversation_state['collected_info']['time'] = time_str
                    info = self.conversation_state['collected_info']
                    
                    await self.assistant.say(
                        f"Perfect! Please confirm your appointment details:\n"
                        f"Name: {info['name']}\n"
                        f"Email: {info['email']}\n"
                        f"Date: {info['date']}\n"
                        f"Time: {time_str}\n"
                        f"Is this correct?", 
                        allow_interruptions=True
                    )
                    self.conversation_state['current_step'] = 'confirmation'
                else:
                    await self.assistant.say("I didn't quite get that time. Could you please provide it in HH:MM format?", allow_interruptions=True)

            elif self.conversation_state['current_step'] == 'confirmation':
                if user_message in ["yes", "yeah", "sure", "okay", "correct", "yep", "right"]:
                    info = self.conversation_state['collected_info']

                    # result = await self.calendar_functions.schedule_appointment(
                    #     name=info['name'],
                    #     email=info['email'],
                    #     date=info['date'],
                    #     time=info['time']
                    # )
                    result = "Great! I've successfully scheduled your appointment."
                    self.conversation_state['appointment_confirmed'] = True
                    await self.assistant.say(f"{result} Is there anything else you need help with?", allow_interruptions=True)

                elif user_message in ["no", "nope", "not now", "nah", "wrong"]:
                    self.conversation_state['current_step'] = 'name'
                    await self.assistant.say("I apologize. Let's start over. Could you please provide your full name?", allow_interruptions=True)

        except Exception as e:
            logger.error(f"Speech processing error: {str(e)}", exc_info=True)
            await self.assistant.say("I'm having trouble understanding. Could you please repeat that?", allow_interruptions=True)

    def _format_date(self, date_str: str) -> Optional[str]:
        """Format date string to YYYY-MM-DD"""
        try:
            date_parts = re.findall(r'\d+|january|february|march|april|may|june|july|august|september|october|november|december', date_str.lower())
            if len(date_parts) >= 3:
                months = {
                    'january': '01', 'february': '02', 'march': '03', 'april': '04',
                    'may': '05', 'june': '06', 'july': '07', 'august': '08',
                    'september': '09', 'october': '10', 'november': '11', 'december': '12'
                }
                year = next(p for p in date_parts if len(p) == 4)
                month = next(p for p in date_parts if p in months or (p.isdigit() and int(p) <= 12))
                day = next(p for p in date_parts if p.isdigit() and int(p) <= 31 and p != year)
                month = months.get(month, f"{int(month):02d}")
                return f"{year}-{month}-{int(day):02d}"
        except Exception:
            pass
        return None

    def _format_time(self, time_str: str) -> Optional[str]:
        """Format time string to HH:MM"""
        try:
            # Extract hour and potential minutes
            if "morning" in time_str or "am" in time_str:
                hour = int(re.findall(r'\d+', time_str)[0])
                return f"{hour:02d}:00"
            elif "afternoon" in time_str or "pm" in time_str:
                hour = int(re.findall(r'\d+', time_str)[0]) + 12
                return f"{hour:02d}:00"
            elif ":" in time_str:
                hour, minute = map(int, re.findall(r'\d+', time_str))
                return f"{hour:02d}:{minute:02d}"
            else:
                hour = int(re.findall(r'\d+', time_str)[0])
                return f"{hour:02d}:00"
        except Exception:
            pass
        return None

    async def fetch_context(self):
        print("Fetching context...")
        base_url = os.getenv("BACKEND_BASE_URL")
        """Fetch organization context from the API"""
        try:
            sessionId = self.context.room.name.replace("call-", "")
            logger.info(f"Fetching context for sessionId: {sessionId}")
            
            async with aiohttp.ClientSession() as session:
                url = f"{base_url}/api/agent/get-session-context?sessionId={sessionId}"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        context = data.get("context", "")
                        logger.info(f"Retrieved context: {context}")
                        
                        print("Context:", context)
                        conversationGuidelines = (
                            # "- Your name or <agent_name> is Gemma\n"

                            "- don't say `how can i assist you today` or similar in the conversation\n"
                            "- Use a friendly, professional tone\n"
                            "- Use context-aware responses\n"
                            "- Allow for natural speech patterns\n"
                            "- Be patient with pauses and corrections\n"
                            "- Don't answer anything more than system-script, you can go little around but stick to the system-script"
                        )
                        
                        script = f"system-script: {context}, conversation-guidelines: {conversationGuidelines}" 

                        if self.assistant and hasattr(self.assistant, 'chat_ctx'):
                            self.assistant.chat_ctx.append(
                                role="system",
                                text=script
                            )
                    else:
                        logger.error(f"Failed to fetch context: HTTP {response.status}")
        except Exception as e:
            logger.error(f"Error fetching context: {e}")

    def setup_dtmf_handler(self):
        """Set up DTMF event handler for the room"""
        @self.context.room.on("sip_dtmf_received")
        def handle_dtmf(dtmf_event: rtc.SipDTMF):
            digit = dtmf_event.digit
            identity = dtmf_event.participant.identity
            logger.info(f"DTMF received - Digit: '{digit}'")

            if digit in DEPARTMENT_NUMBERS:
                env_var, dept_name = DEPARTMENT_NUMBERS[digit]
                transfer_number = f"tel:{os.getenv(env_var)}"
                asyncio.create_task(self.handle_department_transfer(identity, transfer_number, dept_name))
            else:
                asyncio.create_task(self.assistant.say(
                    "I'm sorry, please choose one of the options I mentioned earlier.", 
                    allow_interruptions=True
                ))

    async def handle_department_transfer(self, participant_identity: str, transfer_number: str, department: str) -> None:
        """Handle the transfer process with department-specific messaging"""
        try:
            await self.assistant.say(
                f"Transferring you to our {department} department in a moment. Please hold.", 
                allow_interruptions=False
            )
            await asyncio.sleep(3)
            
            if not self.livekit_api:
                self.livekit_api = api.LiveKitAPI(
                    url=os.getenv('LIVEKIT_URL'),
                    api_key=os.getenv('LIVEKIT_API_KEY'),
                    api_secret=os.getenv('LIVEKIT_API_SECRET')
                )

            transfer_request = proto_sip.TransferSIPParticipantRequest(
                participant_identity=participant_identity,
                room_name=self.context.room.name,
                transfer_to=transfer_number,
                play_dialtone=True
            )

            await self.livekit_api.sip.transfer_sip_participant(transfer_request)
            logger.info(f"Successfully transferred participant {participant_identity} to {transfer_number}")

        except Exception as e:
            logger.error(f"Failed to transfer participant: {e}", exc_info=True)
            await self.assistant.say(
                "I'm sorry, I couldn't transfer your call. Is there something else I can help with?", 
                allow_interruptions=True
            )

    async def start(self):
        """Start the assistant and set up event handlers"""
        try:
            await self.context.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

            self.assistant.start(self.context.room)
            # await asyncio.sleep(1)

            self.setup_dtmf_handler()

            # Fetch context from node baceknd
            await self.fetch_context()

            greeting = "Hello and welcome! how are you doing today!"
            await self.assistant.say(greeting, allow_interruptions=True)

        except Exception as e:
            logger.error(f"Error starting assistant: {e}", exc_info=True)

    async def hang_up(self):
        try:
            if self.context.room:
                logger.info("Disconnecting the call...")
                await self.context.room.disconnect()
        except Exception as e:
            logger.error(f"Failed to hang up: {e}")

    async def cleanup(self):
        """Properly clean up resources and disconnect the assistant."""
        try:
            if self.assistant:
                logger.info("Cleaning up assistant resources...")
                if hasattr(self.assistant.tts, "aclose"):
                    await self.assistant.tts.aclose()  
                
                if hasattr(self.assistant.stt, "aclose"):
                    await self.assistant.stt.aclose()  
                
                self.assistant = None
            
            if self.livekit_api:
                logger.info("Closing LiveKit API connection...")
                await self.livekit_api.aclose()
                self.livekit_api = None

            self.transfer_in_progress = False
            logger.info("Cleanup completed successfully.")
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
        print("Starting voice assistant...")
        await assistant.start()
        await disconnect_event.wait()
    finally:
        await assistant.cleanup()

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
