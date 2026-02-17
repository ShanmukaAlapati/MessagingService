import os
import psycopg2

# DATABASE_URL = os.getenv("DATABASE_URL_EXTERNAL")
DATABASE_URL = "postgresql://messaging_pqn5_user:6V2pydHKjagum5hblgHf9nDdxPWb6EhO@dpg-d6a1mvf5r7bs73fg5rog-a.oregon-postgres.render.com/messaging_pqn5"


USERS = [
    ("sai@example.com", "Sai"),
    ("ankit@example.com", "Ankit"),
    ("ravi@example.com", "Ravi"),
    ("meera@example.com", "Meera"),
    ("john@example.com", "John"),
    ("emma@example.com", "Emma"),
    ("li@example.com", "Li"),
    ("fatima@example.com", "Fatima"),
    ("carlos@example.com", "Carlos"),
    ("sophia@example.com", "Sophia"),
]

def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    # Create users table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
    """)

    for uid, name in USERS:
        cur.execute(
            "INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING;",
            (uid, name)
        )

    conn.commit()
    cur.close()
    conn.close()
    print("Seeded users:", len(USERS))

if __name__ == "__main__":
    main()
