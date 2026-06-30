import os
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

# Load environment variables from .env file (override any existing env vars)
load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """Get PostgreSQL database connection"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set. Please configure your PostgreSQL connection in .env file.")
    
    # Handle both postgres:// and postgresql:// URLs (Render uses postgres://)
    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    # Try with SSL first (for cloud databases like Render, Supabase, etc.)
    try:
        conn = psycopg.connect(db_url, sslmode="require", row_factory=dict_row)
        return conn
    except psycopg.OperationalError:
        # Fallback to no SSL for local PostgreSQL
        try:
            conn = psycopg.connect(db_url, sslmode="prefer", row_factory=dict_row)
            return conn
        except psycopg.OperationalError:
            # Last attempt with no SSL at all
            conn = psycopg.connect(db_url, sslmode="disable", row_factory=dict_row)
            return conn

def get_cursor(conn):
    """Get cursor for PostgreSQL database"""
    return conn.cursor()
