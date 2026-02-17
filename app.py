import os
from datetime import datetime

from flask import Flask, request, jsonify, g, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from flask_cors import CORS

import psycopg2
from psycopg2.extras import RealDictCursor

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")  # set in Render/local env
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "simple-chat-key-change-in-prod")

CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Track if we've run init_db() at least once in this process
_db_initialized = False


# -------------------------------------------------------------------
# DB helpers
# -------------------------------------------------------------------

def get_db():
    """
    Get a per-request Postgres connection using Flask's g.
    """
    if 'db' not in g:
        g.db = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """
    Close DB connection at end of request.
    """
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """
    Ensure tables exist in Postgres: users, conversations, messages.
    Safe to run multiple times (uses IF NOT EXISTS).
    """
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user1_id TEXT NOT NULL,
            user2_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user1_id, user2_id),
            FOREIGN KEY (user1_id) REFERENCES users(id),
            FOREIGN KEY (user2_id) REFERENCES users(id)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            sender_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            FOREIGN KEY (sender_id) REFERENCES users(id)
        );
    """)

    conn.commit()
    cur.close()
    conn.close()


@app.before_request
def ensure_db_initialized():
    """
    Run init_db() once in this process before handling the first request.
    This ensures tables exist before any SELECT/INSERT.
    """
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True


def normalize_pair(a: str, b: str):
    """
    Normalize pair order so (a,b) == (b,a).
    """
    return (a, b) if a < b else (b, a)


def conv_room(conv_id: int) -> str:
    """
    Build Socket.IO room name from conversation id.
    """
    return f"conv_{conv_id}"


# -------------------------------------------------------------------
# REST: seed users (optional)
# -------------------------------------------------------------------

@app.route("/seed-users", methods=["POST"])
def seed_users():
    """
    Seed demo users into the users table.
    Body: {"users": [{"id": "sai@example.com", "name": "Sai"}, ...]}
    """
    data = request.get_json(force=True, silent=True) or {}
    users = data.get("users") or []
    if not users:
        return jsonify({"status": "no_users_given"}), 400

    db = get_db()
    cur = db.cursor()
    for u in users:
        uid = u.get("id")
        name = u.get("name") or uid
        if not uid:
            continue
        cur.execute(
            "INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING;",
            (uid, name),
        )

    db.commit()
    cur.close()
    return jsonify({"status": "ok", "count": len(users)})


# -------------------------------------------------------------------
# REST: users
# -------------------------------------------------------------------

@app.route("/users", methods=["GET"])
def list_users():
    """
    Return all users (id + name) for selection in UI.
    """
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name FROM users ORDER BY name ASC;")
    rows = cur.fetchall()
    users = [dict(id=r["id"], name=r["name"]) for r in rows]
    cur.close()
    return jsonify({"users": users})


# -------------------------------------------------------------------
# REST: conversations
# -------------------------------------------------------------------

@app.route("/conversations/direct", methods=["GET"])
def get_or_create_direct_conversation():
    """
    Get or create a 1-to-1 conversation between me and other.
    Query params: ?me=<user_id>&other=<user_id>
    If users don't exist, you can choose to auto-insert them here.
    """
    current_user = request.args.get("me")
    other_user = request.args.get("other")

    if not current_user or not other_user:
        return jsonify({"error": "me and other query params are required"}), 400

    u1, u2 = normalize_pair(current_user, other_user)
    db = get_db()
    cur = db.cursor()

    # OPTIONAL: auto-insert users if they don't exist (current simple approach)
    # This lets you start using the system from first message without manual seeding.
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING;", (u1, u1))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING;", (u2, u2))
    db.commit()

    # Find existing conversation
    cur.execute(
        "SELECT id FROM conversations WHERE user1_id=%s AND user2_id=%s;",
        (u1, u2),
    )
    row = cur.fetchone()

    if row:
        conv_id = row["id"]
    else:
        cur.execute(
            "INSERT INTO conversations (user1_id, user2_id) VALUES (%s, %s) RETURNING id;",
            (u1, u2),
        )
        conv_id = cur.fetchone()["id"]
        db.commit()

    cur.close()
    return jsonify({"conversation_id": conv_id, "user1_id": u1, "user2_id": u2})


@app.route("/conversations/<int:conv_id>/messages", methods=["GET"])
def get_messages(conv_id: int):
    """
    Fetch recent messages for a conversation.
    ?limit=N (default 50), newest last.
    """
    limit = int(request.args.get("limit", 50))
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, sender_id, text, created_at
        FROM messages
        WHERE conversation_id=%s
        ORDER BY id DESC
        LIMIT %s;
        """,
        (conv_id, limit),
    )
    rows = cur.fetchall()
    cur.close()

    messages = [
        {
            "id": r["id"],
            "sender": r["sender_id"],
            "text": r["text"],
            "created_at": r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else str(r["created_at"]),
        }
        for r in reversed(rows)
    ]
    return jsonify({"conversation_id": conv_id, "messages": messages})


# -------------------------------------------------------------------
# Socket.IO events
# -------------------------------------------------------------------

@socketio.on("join_conversation")
def handle_join_conversation(data):
    """
    Join a 1-to-1 conversation room and send recent history to the client.
    data: {conversation_id: int, user_id: str}
    """
    conv_id = int(data.get("conversation_id"))
    user_id = data.get("user_id")
    room = conv_room(conv_id)

    join_room(room)
    print(f"[SOCKET] User {user_id} joined conversation {conv_id} -> room {room}")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, sender_id, text, created_at
        FROM messages
        WHERE conversation_id=%s
        ORDER BY id DESC
        LIMIT 50;
        """,
        (conv_id,),
    )
    rows = cur.fetchall()
    cur.close()

    history = [
        {
            "id": r["id"],
            "sender": r["sender_id"],
            "text": r["text"],
            "created_at": r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else str(r["created_at"]),
        }
        for r in reversed(rows)
    ]

    emit("chat_history", {"conversation_id": conv_id, "messages": history})


@socketio.on("send_message")
def handle_send_message(data):
    """
    Insert a new message and broadcast it to all sockets in that conversation.
    data: {conversation_id: int, sender_id: str, text: str}
    """
    conv_id = int(data.get("conversation_id"))
    sender = data.get("sender_id")
    text = (data.get("text") or "").strip()

    if not sender or not text:
        return

    db = get_db()
    cur = db.cursor()

    # OPTIONAL: ensure sender exists in users table
    cur.execute(
        "INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING;",
        (sender, sender),
    )

    cur.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, text)
        VALUES (%s, %s, %s)
        RETURNING id, created_at;
        """,
        (conv_id, sender, text),
    )
    row = cur.fetchone()
    db.commit()
    cur.close()

    msg_id = row["id"]
    created_at = row["created_at"]

    msg_payload = {
        "id": msg_id,
        "conversation_id": conv_id,
        "sender": sender,
        "text": text,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
    }

    room = conv_room(conv_id)
    print(f"[SOCKET] Message in conv {conv_id} ({room}) from {sender}: {text}")

    emit("new_message", msg_payload, room=room)


# -------------------------------------------------------------------
# Static UI
# -------------------------------------------------------------------

@app.route("/chat")
def chat_ui():
    """
    Serve the chat interface HTML from ./static/chat.html.
    """
    return send_from_directory(".", "chat.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# -------------------------------------------------------------------
# Entry point (local dev)
# -------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
