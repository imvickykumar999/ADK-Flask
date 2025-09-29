import os
import asyncio
import json
import sqlite3
import secrets # Import for generating secure session IDs
from dotenv import load_dotenv
# FIX: Added 'Response' to the import list
from flask import Flask, request, jsonify, g, redirect, url_for, make_response, Response 
# MODIFIED: Use DatabaseSessionService for persistent sessions
from google.adk.sessions import DatabaseSessionService 
from google.adk.runners import Runner
from google.genai.types import Content, Part

# NOTE: The 'instance.agent' module is assumed to be available in the execution environment.
# Ensure 'instance/agent.py' exists and exports a 'root_agent' instance for this to work.
try:
    # If using ADK, this is where your agent definition lives
    from instance.agent import root_agent
except ImportError:
    print("WARNING: 'instance.agent' could not be imported. Agent functionality will be disabled.")
    root_agent = None

# Load environment variables from .env file
load_dotenv()

# --- ADK Initialization & Global State ---
APP_NAME = "agent_flask"
USER_ID = "web_user" # Keeping a fixed user ID for this web demo

# --- Database Configuration ---
# CONSOLIDATED: Use a single file name for both the UI history and the ADK session service.
DATABASE = 'history.db'
# MODIFIED: Database URL for ADK Session Persistence now points to the same file
DB_URL = os.getenv("SESSION_DB_URL", f"sqlite:///./{DATABASE}")

# Initialize Flask App EARLY to ensure it's available for decorators
app = Flask(__name__)

# --- Database Functions ---

def get_db():
    """Returns a database connection, creating one if not present in flask.g."""
    if 'db' not in g:
        # This function connects to history.db
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row  # Allows accessing columns by name
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    """Closes the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    """Initializes the database and creates the messages table."""
    with app.app_context():
        db = get_db()
        # Create a table to store chat messages (for UI history display)
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL, -- 'user' or 'agent'
                text TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

def save_message(session_id: str, role: str, text: str):
    """Saves a single message to the database."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)",
            (session_id, role, text)
        )
        db.commit()
    except Exception as e:
        app.logger.error(f"Database Save Error: {e}")

def load_history(session_id: str) -> list[dict]:
    """Loads all messages for a given session ID."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT role, text FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()
        return [{"role": row['role'], "text": row['text']} for row in rows]
    except Exception as e:
        app.logger.error(f"Database Load Error: {e}")
        return []

def get_all_session_ids() -> list[str]:
    """Loads all unique session IDs from the database."""
    try:
        db = get_db()
        rows = db.execute(
            # Order by timestamp of the last message in that session for better display logic
            """
            SELECT T1.session_id FROM messages T1
            INNER JOIN (
                SELECT session_id, MAX(timestamp) AS max_timestamp
                FROM messages
                GROUP BY session_id
            ) T2 ON T1.session_id = T2.session_id AND T1.timestamp = T2.max_timestamp
            GROUP BY T1.session_id
            ORDER BY T2.max_timestamp DESC
            """
        ).fetchall()
        return [row['session_id'] for row in rows]
    except Exception as e:
        app.logger.error(f"Database Session Load Error: {e}")
        return []


# MODIFIED: Initialize DatabaseSessionService using the consolidated DB_URL
session_service = DatabaseSessionService(db_url=DB_URL)

# Create the runner with the agent only if root_agent was successfully imported
runner = None
# We no longer need adk_sessions dictionary to track initialization, 
# as DatabaseSessionService manages persistence, but we keep it for now for simplicity 
adk_sessions = {} # Dictionary to track which sessions have been accessed since restart

if root_agent:
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    async def initialize_adk_session(session_id: str):
        """
        Ensures the ADK session is accessible and created if it doesn't exist.
        The DatabaseSessionService handles loading persistent history.
        """
        if session_id not in adk_sessions:
            app.logger.info(f"Initializing ADK session check for {USER_ID}/{session_id}")
            
            try:
                # FIX: Corrected the way arguments are passed to get_session. 
                # Using keyword arguments for robustness.
                
                session = await session_service.get_session(
                    app_name=APP_NAME, 
                    user_id=USER_ID, 
                    session_id=session_id
                )
                
                if not session:
                    # FIX: Removed the unexpected 'history=[]' argument from create_session call
                    await session_service.create_session(
                        app_name=APP_NAME,
                        user_id=USER_ID,
                        session_id=session_id
                    )

            except Exception as e:
                 # Catch initialization errors specific to the DatabaseSessionService
                app.logger.error(f"DatabaseSessionService Initialization Error: {e}")
                raise 
            
            adk_sessions[session_id] = True


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
    """Returns the chat history and all sessions for the current session ID."""
    current_session_id = request.args.get('session_id')
    if not current_session_id:
        return jsonify({"history": [], "sessions": []}), 200

    history = load_history(current_session_id)
    sessions = get_all_session_ids()
    
    return jsonify({
        "history": history,
        "current_session_id": current_session_id,
        "sessions": sessions
    })

@app.route('/chat', methods=['POST'])
def chat():
    """Handles incoming user messages, runs the ADK agent, and returns the response."""
    current_session_id = request.args.get('session_id')
    if not current_session_id:
        return jsonify({"response": "Error: Session ID is missing."}), 400

    if not runner:
        return jsonify({"response": "Error: Agent runner is not initialized. Check server logs."}), 500

    # Ensure the ADK session is initialized/loaded from the database
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

    # 1. Save user message to UI history DB (history.db)
    save_message(current_session_id, "user", user_input)

    # Prepare the message for the runner
    message = Content(role="user", parts=[Part(text=user_input)])

    response_text = "Sorry, I encountered an internal error."

    async def get_agent_response(msg, session_id):
        """Asynchronously runs the agent and extracts the final text response."""
        response = ""
        try:
            async for event in runner.run_async(
                user_id=USER_ID,
                session_id=session_id,
                new_message=msg
            ):
                if hasattr(event, "is_final_response") and event.is_final_response():
                    if hasattr(event, "content") and event.content.parts:
                        # Extract text from the first part of the content
                        response = event.content.parts[0].text
                        break
        except Exception as e:
            # Handle potential ADK/Runner exceptions
            return f"An agent error occurred: {str(e)}"
        
        return response

    try:
        final_response = asyncio.run(get_agent_response(message, current_session_id))
        
        if final_response.startswith("An agent error occurred"):
            response_text = final_response
            status_code = 500
        else:
            response_text = final_response
            status_code = 200
            
            # 2. Save agent message to UI history DB (history.db) on success
            # The agent's history is automatically saved by DatabaseSessionService
            save_message(current_session_id, "agent", response_text)

    except Exception as e:
        response_text = f"Flask runtime error: {str(e)}"
        status_code = 500
        
    return jsonify({"response": response_text}), status_code


@app.route('/')
def index():
    """Handles dynamic session creation and serves the main chat application page."""
    
    # 1. Check for/create session_id and handle redirect if needed
    session_id_result = get_or_create_session_id()
    # FIX: Check against the Flask Response class type, as flask.redirect() returns a Response object.
    if isinstance(session_id_result, Response): 
        return session_id_result
    
    current_session_id = session_id_result

    # 2. Pass the current session ID to the HTML generator
    html_content = get_html_content(current_session_id)
    response = make_response(html_content)
    return response

# --- Frontend HTML/JS/CSS (Inlined for single-file deployment) ---

def get_html_content(current_session_id):
    """Generates the single HTML page with inline CSS and JavaScript for the chat UI."""
    
    html_template = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ADK Agent Web Chat - Session {current_session_id}</title>
        <!-- Load Tailwind CSS CDN -->
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            /* Custom Scrollbar for Chat Window */
            .chat-window::-webkit-scrollbar {{
                width: 8px;
            }}
            .chat-window::-webkit-scrollbar-thumb {{
                background-color: #a3a3a3; /* gray-400 */
                border-radius: 4px;
            }}
            .chat-window::-webkit-scrollbar-track {{
                background-color: #f3f4f6; /* gray-100 */
            }}
            /* Use Inter font */
            body {{
                font-family: 'Inter', sans-serif;
            }}
        </style>
        <script>
            // Set up Tailwind configuration for the Inter font
            tailwind.config = {{
                theme: {{
                    extend: {{
                        fontFamily: {{
                            sans: ['Inter', 'sans-serif'],
                        }}
                    }}
                }}
            }}
        </script>
    </head>
    <!-- MODIFIED: Wrapper for the whole layout, using vh to fill screen -->
    <body class="bg-gray-100 min-h-screen flex w-full"> 
        
        <!-- Sidebar for Session History -->
        <!-- MODIFIED: Fixed sidebar on mobile, static on desktop. 
                     Uses transform to hide/show off-screen on mobile. -->
        <div id="sidebar" class="fixed top-0 left-0 h-screen w-64 bg-white shadow-2xl ring-1 ring-gray-200 overflow-y-auto p-4 flex-shrink-0 z-50 
               transform -translate-x-full transition-transform duration-300
               lg:static lg:transform-none lg:h-[calc(100vh-2rem)] lg:mt-4 lg:mb-4 lg:ml-4 lg:mr-0 lg:rounded-xl">
            <h2 class="text-xl font-bold text-gray-800 mb-4 border-b pb-2">Sessions</h2>
            <a href="/" id="new-chat-link" class="block w-full text-center py-2 mb-4 bg-green-500 text-white font-semibold rounded-lg hover:bg-green-600 transition duration-200">
                + New Chat
            </a>
            <div id="session-list" class="space-y-1">
                <!-- Session links will be populated here -->
            </div>
        </div>
        
        <!-- Main Chat Area -->
        <!-- MODIFIED: Full width (w-full) on all screens, uses flex-grow to take space. 
                     Uses h-screen on mobile and h-[calc(100vh-2rem)] on desktop for better fit. -->
        <div class="w-full bg-white shadow-2xl ring-1 ring-gray-200 rounded-none overflow-hidden flex flex-col h-screen flex-grow 
             lg:h-[calc(100vh-2rem)] lg:mt-4 lg:mb-4 lg:mr-4 lg:rounded-xl">
            
            <!-- Header -->
            <!-- ADDED: Hamburger button and flex layout to place it on the left -->
            <header class="p-4 bg-indigo-600 text-white shadow-lg rounded-t-none lg:rounded-t-xl flex items-center">
                <!-- Hamburger Button, visible only below large screen size -->
                <button id="menu-button" class="lg:hidden p-2 mr-3 rounded-md hover:bg-indigo-700 transition duration-200 focus:outline-none focus:ring-2 focus:ring-white">
                    <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"></path></svg>
                </button>
                <div class="flex-grow">
                    <h1 class="text-2xl font-extrabold tracking-tight">ADK Agent Chat</h1>
                    <p class="text-sm opacity-80 mt-1">Current Session: <b id="current-session-display">{current_session_id}</b></p>
                </div>
            </header>

            <!-- Chat Window (Content will be filled by JavaScript on load) -->
            <div id="chat-window" class="chat-window flex-grow overflow-y-auto p-4 space-y-4">
                <!-- Chat messages go here -->
            </div>

            <!-- Input Form -->
            <div class="p-4 border-t border-gray-200 bg-white">
                <form id="chat-form" class="flex space-x-3">
                    <input 
                        type="text" 
                        id="user-input" 
                        placeholder="Type your message here..." 
                        required
                        autocomplete="off"
                        class="flex-grow p-3 border-2 border-gray-300 rounded-xl focus:ring-indigo-500 focus:border-indigo-500 transition duration-200"
                    />
                    <button 
                        type="submit" 
                        id="send-button"
                        class="bg-indigo-600 text-white p-3 rounded-xl font-bold shadow-lg hover:bg-indigo-700 transition duration-200 active:scale-[0.98] disabled:bg-gray-400"
                    >
                        Send
                    </button>
                </form>
            </div>
        </div>

        <script>
            document.addEventListener('DOMContentLoaded', () => {{
                const currentSessionId = "{current_session_id}";
                const form = document.getElementById('chat-form');
                const userInput = document.getElementById('user-input');
                const chatWindow = document.getElementById('chat-window');
                const sendButton = document.getElementById('send-button');
                const sidebar = document.getElementById('sidebar');
                const menuButton = document.getElementById('menu-button');
                const sessionList = document.getElementById('session-list');

                // --- Sidebar Logic ---
                const overlay = document.createElement('div');
                overlay.className = 'fixed inset-0 bg-black opacity-0 transition-opacity duration-300 z-40 lg:hidden pointer-events-none';
                document.body.appendChild(overlay);

                let isSidebarOpen = false;

                function toggleSidebar(open) {{
                    // Only run on mobile (screen width < 1024px, the 'lg' breakpoint)
                    if (window.innerWidth >= 1024) return;
                    
                    isSidebarOpen = (open !== undefined) ? open : !isSidebarOpen;

                    if (isSidebarOpen) {{
                        sidebar.classList.remove('-translate-x-full');
                        overlay.classList.remove('opacity-0', 'pointer-events-none');
                        overlay.classList.add('opacity-50', 'pointer-events-auto');
                    }} else {{
                        sidebar.classList.add('-translate-x-full');
                        overlay.classList.remove('opacity-50', 'pointer-events-auto');
                        overlay.classList.add('opacity-0', 'pointer-events-none');
                    }}
                }}

                menuButton.addEventListener('click', () => {{
                    toggleSidebar();
                }});
                
                // Close sidebar when clicking the overlay
                overlay.addEventListener('click', () => {{
                    toggleSidebar(false);
                }});

                // --- End Sidebar Logic ---

                // Function to add a message to the chat window
                function addMessage(text, role) {{
                    const isUser = role === 'user';
                    const messageElement = document.createElement('div');
                    
                    if (isUser) {{
                        messageElement.className = 'flex justify-end';
                        messageElement.innerHTML = `
                            <div class="bg-indigo-600 text-white p-4 rounded-xl rounded-br-sm max-w-[80%] shadow-lg break-words whitespace-pre-wrap">
                                <!-- Placeholder will be filled with text content -->
                            </div>
                        `;
                    }} else {{ // agent
                        messageElement.className = 'flex justify-start';
                        messageElement.innerHTML = `
                            <div class="bg-gray-200 text-gray-800 p-4 rounded-xl rounded-tl-sm max-w-[80%] shadow-lg break-words whitespace-pre-wrap">
                                <!-- Placeholder will be filled with text content -->
                            </div>
                        `;
                    }}
                    
                    // Sanitize text and handle HTML content
                    const contentDiv = messageElement.querySelector('div:last-child');
                    
                    if (role === 'agent' && text.includes('<b')) {{
                        contentDiv.innerHTML = text; 
                    }} else {{
                        contentDiv.textContent = text;
                    }}
                    
                    chatWindow.appendChild(messageElement);
                    // Scroll to the latest message
                    chatWindow.scrollTop = chatWindow.scrollHeight;
                }}

                // Function to populate the sidebar with session links
                function populateSessionList(sessions) {{
                    sessionList.innerHTML = ''; // Clear existing list
                    sessions.forEach(sessionId => {{
                        const link = document.createElement('a');
                        link.href = `/?session_id=${'{sessionId}'}`;
                        
                        link.className = `block p-2 text-sm rounded-lg hover:bg-gray-200 transition duration-150 truncate \
                            $${{sessionId}} === currentSessionId ? 'bg-indigo-100 font-semibold text-indigo-700' : 'text-gray-700'`;
                        
                        link.textContent = `Chat #$${{sessionId}}`; // Escaped again for text content
                        link.addEventListener('click', () => toggleSidebar(false)); // Close sidebar on session change
                        sessionList.appendChild(link);
                    }});
                }}
                
                // Function to load and display chat history and sessions
                async function loadChatData() {{
                    try {{
                        const response = await fetch(`/history?session_id=${'{currentSessionId}'}`);
                        const data = await response.json();
                        
                        // 1. Clear chat window first
                        chatWindow.innerHTML = '';

                        // 2. Load History
                        const history = data.history || [];
                        if (history.length === 0) {{
                            addMessage(`Welcome to Chat #<b class='text-indigo-700'>${'{currentSessionId}'}</b>! I am your ADK Agent, ready to assist you. Ask me anything!`, 'agent');
                        }} else {{
                            history.forEach(msg => {{
                                if (msg.role && msg.text) {{
                                    addMessage(msg.text, msg.role);
                                }}
                            }});
                        }}
                        
                        // 3. Populate Session List
                        populateSessionList(data.sessions || []);

                    }} catch (error) {{
                        console.error('Failed to load chat data:', error);
                        // Fallback welcome message
                        chatWindow.innerHTML = '';
                        addMessage('Network or database error. Please refresh. If this is a new session, you can start chatting.', 'agent');
                    }}
                }}

                // Load chat data when the page loads
                loadChatData();

                // Function to show a loading state
                function showLoading() {{
                    let loadingDiv = document.getElementById('loading-message');
                    if (!loadingDiv) {{
                        loadingDiv = document.createElement('div');
                        loadingDiv.id = 'loading-message';
                        loadingDiv.className = 'flex justify-start';
                        loadingDiv.innerHTML = `
                            <div class="bg-gray-200 text-gray-600 p-4 rounded-xl rounded-tl-sm max-w-[80%] shadow-md">
                                <span class="animate-pulse">Agent is thinking...</span>
                            </div>
                        `;
                        chatWindow.appendChild(loadingDiv);
                        chatWindow.scrollTop = chatWindow.scrollHeight;
                    }}
                    return loadingDiv;
                }}

                // Function to hide the loading state
                function hideLoading() {{
                    const loadingDiv = document.getElementById('loading-message');
                    if (loadingDiv) {{
                        loadingDiv.remove();
                    }}
                }}

                // Handle form submission
                form.addEventListener('submit', async (e) => {{
                    e.preventDefault();
                    
                    const message = userInput.value.trim();
                    if (!message) return;

                    // 1. Display user message
                    addMessage(message, 'user');
                    
                    // 2. Clear input and disable input/button
                    userInput.value = '';
                    sendButton.disabled = true;
                    userInput.disabled = true;

                    // 3. Show loading indicator
                    showLoading();

                    try {{
                        // 4. Send message to Flask backend, including session_id in the query
                        const response = await fetch(`/chat?session_id=${'{currentSessionId}'}`, {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }},
                            body: JSON.stringify({{ message: message }})
                        }});

                        const data = await response.json();

                        // 5. Hide loading indicator
                        hideLoading();

                        // 6. Display agent response
                        if (response.ok) {{
                            addMessage(data.response, 'agent');
                            // After a successful chat, reload session list in case a new message updates the session list
                            loadChatData(); 
                        }} else {{
                            addMessage(`Error: ${'{data.response}'}`, 'agent');
                            console.error('Agent API Error:', data.response);
                        }}

                    }} catch (error) {{
                        // 5. Hide loading indicator on error
                        hideLoading();
                        // 6. Display network error
                        addMessage('Network Error: Could not reach the server.', 'agent');
                        console.error('Fetch Error:', error);
                    }} finally {{
                        // 7. Re-enable input/button
                        sendButton.disabled = false;
                        userInput.disabled = false;
                        userInput.focus();
                    }}
                }});
            }});
        </script>
    </body>
    </html>
    """
    return html_template

# --- Run the Flask App ---
if __name__ == "__main__":
    # Initialize database when the application starts
    init_db()
    # To run this file, you'll need to:
    # 1. Have 'instance/agent.py' (or mock it)
    # 2. Install dependencies (flask, python-dotenv, google-genai, google-adk)
    # 3. Run from the terminal: python app.py
    app.run(debug=True, host='0.0.0.0', port=5000)
