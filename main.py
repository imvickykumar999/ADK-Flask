import os
import asyncio
import secrets
from dotenv import load_dotenv
from flask import Flask, request, jsonify, redirect, url_for, make_response, Response, render_template

# MODIFIED: Use InMemorySessionService - history will not persist after server restarts
from google.adk.sessions import InMemorySessionService 
from google.adk.runners import Runner
from google.genai.types import Content, Part

# NOTE: Fixed import path to the standard 'instance.agent'
try:
    from templates.agent import root_agent
except ImportError:
    print("WARNING: 'instance.agent' could not be imported. Agent functionality will be disabled.")
    root_agent = None

# Load environment variables from .env file
load_dotenv()

# --- ADK Initialization & Global State ---
APP_NAME = "agent_flask"
USER_ID = "web_user" # Fixed user ID for this demo

# Initialize Flask App
app = Flask(__name__)

# --- ADK Session Service (In-Memory) ---

# Initialize Session Service (No external DB required, persistence is only for current server lifespan)
session_service = InMemorySessionService()

# Create the runner with the agent only if root_agent was successfully imported
runner = None
adk_sessions = {} # Dictionary to track which sessions are currently active in memory

if root_agent:
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # --- ADK Asynchronous Helper Functions (for UI) ---

    async def initialize_adk_session(session_id: str):
        """Ensures the ADK session is accessible and created if it doesn't exist."""
        if session_id not in adk_sessions:
            try:
                session = await session_service.get_session(
                    app_name=APP_NAME, 
                    user_id=USER_ID, 
                    session_id=session_id
                )
                
                if not session:
                    # Create session if it doesn't exist
                    await session_service.create_session(
                        app_name=APP_NAME,
                        user_id=USER_ID,
                        session_id=session_id
                    )
            except Exception as e:
                app.logger.error(f"InMemorySessionService Initialization Error: {e}")
                raise 
            
            adk_sessions[session_id] = True

    async def load_adk_history(session_id: str) -> list[dict]:
        """Loads chat history for a given session directly from the ADK service (in memory)."""
        try:
            session = await session_service.get_session(
                app_name=APP_NAME, user_id=USER_ID, session_id=session_id
            )
            if not session or not session.history:
                return []
            
            history_list = []
            for content in session.history:
                # ADK history contains Content objects, we need to extract the text part
                text = content.parts[0].text if content.parts and content.parts[0].text else "[Content Missing]"
                history_list.append({"role": content.role, "text": text})

            return history_list
        except Exception as e:
            app.logger.error(f"ADK History Load Error: {e}")
            return []

    async def load_all_adk_sessions() -> list[str]:
        """Loads all known session IDs currently active in memory for UI display."""
        # We use the keys of the adk_sessions dict to represent all active sessions
        return sorted(list(adk_sessions.keys()), reverse=True)


# --- Helper to get/create session ID from request ---
def get_or_create_session_id():
    """Gets the session ID from the request or generates a new one."""
    session_id = request.args.get('session_id')
    if not session_id:
        # Generate a new, short, URL-safe session ID (e.g., 'a3b7c4d8')
        session_id = secrets.token_hex(4)
        # Redirect to the new URL with the session_id query parameter
        return redirect(url_for('index', session_id=session_id))
    return session_id

# --- API Endpoints ---

@app.route('/history', methods=['GET'])
def get_history_api():
    """Returns the chat history and all sessions for the current session ID by fetching data from memory."""
    current_session_id = request.args.get('session_id')
    if not current_session_id or not runner:
        return jsonify({"history": [], "sessions": []}), 200

    try:
        # Run async functions synchronously
        history = asyncio.run(load_adk_history(current_session_id))
        sessions = asyncio.run(load_all_adk_sessions())
        
        return jsonify({
            "history": history,
            "current_session_id": current_session_id,
            "sessions": sessions
        })
    except Exception as e:
        app.logger.error(f"API History Endpoint Error: {e}")
        return jsonify({
            "error": "Failed to load history from memory.",
            "history": [],
            "sessions": []
        }), 500


@app.route('/chat', methods=['POST'])
def chat():
    """Handles incoming user messages, runs the ADK agent, and returns the response."""
    current_session_id = request.args.get('session_id')
    if not current_session_id:
        return jsonify({"response": "Error: Session ID is missing."}), 400

    if not runner:
        return jsonify({"response": "Error: Agent runner is not initialized. Check server logs."}), 500

    # Ensure the ADK session is initialized/loaded into memory
    if root_agent and current_session_id not in adk_sessions:
        try:
            # Synchronously call the async session initializer
            asyncio.run(initialize_adk_session(current_session_id))
        except Exception as e:
            app.logger.error(f"ADK Session Initialization Error: {e}")
            return jsonify({"response": f"ADK Session Init Error: {str(e)}"}), 500

    data = request.get_json()
    user_input = data.get('message', '').strip()

    if not user_input:
        return jsonify({"response": "Please provide a message."}), 400

    # Prepare the message for the runner
    message = Content(role="user", parts=[Part(text=user_input)])

    response_text = "Sorry, I encountered an internal error."

    async def get_agent_response(msg, session_id):
        """Asynchronously runs the agent and extracts the final text response."""
        response_parts = []
        try:
            async for event in runner.run_async(
                user_id=USER_ID,
                session_id=session_id,
                new_message=msg
            ):
                if hasattr(event, "is_final_response") and event.is_final_response():
                    if hasattr(event, "content") and event.content.parts:
                        # Extract text from the first part of the content
                        response_parts.append(event.content.parts[0].text)
                        break
        except Exception as e:
            # Handle potential ADK/Runner exceptions
            return f"An agent error occurred: {str(e)}"
        
        return "".join(response_parts)

    try:
        final_response = asyncio.run(get_agent_response(message, current_session_id))
        
        if final_response.startswith("An agent error occurred"):
            response_text = final_response
            status_code = 500
        else:
            response_text = final_response
            status_code = 200

    except Exception as e:
        response_text = f"Flask runtime error: {str(e)}"
        status_code = 500
        
    return jsonify({"response": response_text}), status_code


@app.route('/')
def index():
    """Handles dynamic session creation and serves the main chat application page."""
    
    session_id_result = get_or_create_session_id()
    
    if isinstance(session_id_result, Response): 
        return session_id_result
    
    current_session_id = session_id_result

    # Render the index.html template, passing the current session ID
    return render_template('index.html', current_session_id=current_session_id)

# --- Run the Flask App ---
if __name__ == "__main__":
    # The application is now fully independent of any database setup.
    app.run(debug=True, host='0.0.0.0', port=5000)
