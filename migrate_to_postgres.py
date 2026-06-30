"""
Migration Script: SQLite to PostgreSQL
This script migrates all data from the local SQLite database to PostgreSQL.

Usage:
1. Ensure your .env file has the DATABASE_URL set to your PostgreSQL connection string
2. Run: python migrate_to_postgres.py

The script will:
- Create the database if it doesn't exist
- Create tables in PostgreSQL if they don't exist
- Migrate all users from SQLite to PostgreSQL
- Migrate all worklog entries from SQLite to PostgreSQL
- Skip duplicates to avoid errors on re-run
"""

import sqlite3
import psycopg
import os
import re
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_SQLITE = "worklog.db"

def parse_database_url(url):
    """Parse DATABASE_URL to extract components"""
    # Handle both postgres:// and postgresql:// URLs
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    
    # Pattern: postgresql://user:password@host:port/database
    pattern = r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)'
    match = re.match(pattern, url)
    
    if match:
        return {
            'user': match.group(1),
            'password': match.group(2),
            'host': match.group(3),
            'port': match.group(4),
            'database': match.group(5)
        }
    return None

def create_database_if_not_exists():
    """Create the database if it doesn't exist"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set.")
    
    parsed = parse_database_url(DATABASE_URL)
    if not parsed:
        print("⚠ Could not parse DATABASE_URL. Attempting direct connection...")
        return False
    
    db_name = parsed['database']
    
    # Connect to default 'postgres' database to create our database
    try:
        conn = psycopg.connect(
            host=parsed['host'],
            port=parsed['port'],
            user=parsed['user'],
            password=parsed['password'],
            database='postgres',
            sslmode='prefer'
        )
        conn.autocommit = True
        cursor = conn.cursor()
        
        # Check if database exists
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        exists = cursor.fetchone()
        
        if not exists:
            print(f"📦 Creating database '{db_name}'...")
            cursor.execute(f'CREATE DATABASE "{db_name}"')
            print(f"✓ Database '{db_name}' created successfully")
        else:
            print(f"✓ Database '{db_name}' already exists")
        
        cursor.close()
        conn.close()
        return True
        
    except psycopg.Error as e:
        print(f"⚠ Could not create database automatically: {e}")
        return False

def get_postgres_connection():
    """Get PostgreSQL connection"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set. Please set it in your .env file.")
    
    # Handle both postgres:// and postgresql:// URLs
    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    # Try with SSL first (for cloud databases like Render, Supabase, etc.)
    try:
        conn = psycopg.connect(db_url, sslmode="require")
        return conn
    except psycopg.OperationalError:
        # Fallback to no SSL for local PostgreSQL
        try:
            conn = psycopg.connect(db_url, sslmode="prefer")
            return conn
        except psycopg.OperationalError:
            # Last attempt with no SSL at all
            conn = psycopg.connect(db_url, sslmode="disable")
            return conn

def get_sqlite_connection():
    """Get SQLite connection"""
    if not os.path.exists(LOCAL_SQLITE):
        raise FileNotFoundError(f"SQLite database '{LOCAL_SQLITE}' not found.")
    
    conn = sqlite3.connect(LOCAL_SQLITE)
    conn.row_factory = sqlite3.Row
    return conn

def init_postgres_tables(pg_conn):
    """Create tables in PostgreSQL if they don't exist"""
    cursor = pg_conn.cursor()
    
    # Create users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            emp_id VARCHAR(50),
            email VARCHAR(255),
            gender VARCHAR(20),
            designation VARCHAR(255),
            department VARCHAR(255),
            role VARCHAR(50)
        )
    """)
    
    # Create worklog table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS worklog (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            date VARCHAR(20),
            status VARCHAR(50),
            category TEXT,
            task TEXT
        )
    """)
    
    # Create indexes for better performance
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_worklog_username ON worklog(username)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_worklog_date ON worklog(date)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)
    """)
    
    pg_conn.commit()
    print("✓ PostgreSQL tables created/verified successfully")

def migrate_users(sqlite_conn, pg_conn):
    """Migrate users from SQLite to PostgreSQL"""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Fetch all users from SQLite
    sqlite_cursor.execute("SELECT * FROM users")
    users = sqlite_cursor.fetchall()
    
    migrated = 0
    skipped = 0
    
    for user in users:
        try:
            # Check if user already exists in PostgreSQL
            pg_cursor.execute("SELECT id FROM users WHERE username = %s", (user['username'],))
            if pg_cursor.fetchone():
                skipped += 1
                continue
            
            # Insert user into PostgreSQL
            pg_cursor.execute("""
                INSERT INTO users (username, password, emp_id, email, gender, designation, department, role)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user['username'],
                user['password'],
                user['emp_id'],
                user['email'],
                user['gender'],
                user['designation'],
                user['department'],
                user['role']
            ))
            migrated += 1
            
        except Exception as e:
            print(f"  ⚠ Error migrating user '{user['username']}': {e}")
            pg_conn.rollback()
    
    pg_conn.commit()
    print(f"✓ Users migrated: {migrated} new, {skipped} skipped (already exist)")

def migrate_worklog(sqlite_conn, pg_conn):
    """Migrate worklog entries from SQLite to PostgreSQL"""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Fetch all worklog entries from SQLite
    sqlite_cursor.execute("SELECT * FROM worklog")
    entries = sqlite_cursor.fetchall()
    
    migrated = 0
    skipped = 0
    
    for entry in entries:
        try:
            # Check if entry already exists (by username and date)
            pg_cursor.execute(
                "SELECT id FROM worklog WHERE username = %s AND date = %s",
                (entry['username'], entry['date'])
            )
            if pg_cursor.fetchone():
                skipped += 1
                continue
            
            # Insert worklog entry into PostgreSQL
            pg_cursor.execute("""
                INSERT INTO worklog (username, date, status, category, task)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                entry['username'],
                entry['date'],
                entry['status'],
                entry['category'],
                entry['task']
            ))
            migrated += 1
            
        except Exception as e:
            print(f"  ⚠ Error migrating worklog entry for '{entry['username']}' on {entry['date']}: {e}")
            pg_conn.rollback()
    
    pg_conn.commit()
    print(f"✓ Worklog entries migrated: {migrated} new, {skipped} skipped (already exist)")

def ensure_admin_exists(pg_conn):
    """Ensure default admin user exists in PostgreSQL"""
    cursor = pg_conn.cursor()
    
    cursor.execute("SELECT id FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO users (username, password, emp_id, email, gender, designation, department, role)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            'admin',
            generate_password_hash('admin123'),
            'CU0001',
            'admin@university.edu',
            'Male',
            'Director, IQAC',
            'IQAC',
            'Admin'
        ))
        pg_conn.commit()
        print("✓ Default admin user created in PostgreSQL")
    else:
        print("✓ Admin user already exists in PostgreSQL")

def verify_migration(pg_conn):
    """Verify the migration was successful"""
    cursor = pg_conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM worklog")
    worklog_count = cursor.fetchone()[0]
    
    print(f"\n📊 PostgreSQL Database Summary:")
    print(f"   - Total users: {user_count}")
    print(f"   - Total worklog entries: {worklog_count}")

def main():
    print("=" * 60)
    print("IQAC Worklog - SQLite to PostgreSQL Migration")
    print("=" * 60)
    
    # Check if DATABASE_URL is set
    if not DATABASE_URL:
        print("\n❌ ERROR: DATABASE_URL environment variable is not set.")
        print("Please add the following to your .env file:")
        print("DATABASE_URL=postgresql://username:password@host:port/database")
        return
    
    print(f"\n📁 Source: SQLite ({LOCAL_SQLITE})")
    print(f"🐘 Target: PostgreSQL")
    
    try:
        # Try to create database if it doesn't exist
        print("\n🔧 Checking/creating database...")
        create_database_if_not_exists()
        
        # Connect to PostgreSQL
        print("\n🔗 Connecting to PostgreSQL...")
        pg_conn = get_postgres_connection()
        print("✓ Connected to PostgreSQL")
        
        # Initialize PostgreSQL tables
        print("\n📋 Creating/verifying PostgreSQL tables...")
        init_postgres_tables(pg_conn)
        
        # Check if SQLite database exists
        if os.path.exists(LOCAL_SQLITE):
            print(f"\n📂 Found SQLite database, starting migration...")
            sqlite_conn = get_sqlite_connection()
            
            # Migrate users
            print("\n👥 Migrating users...")
            migrate_users(sqlite_conn, pg_conn)
            
            # Migrate worklog
            print("\n📝 Migrating worklog entries...")
            migrate_worklog(sqlite_conn, pg_conn)
            
            sqlite_conn.close()
        else:
            print(f"\n📂 No SQLite database found ({LOCAL_SQLITE})")
            print("   Creating fresh PostgreSQL database...")
        
        # Ensure admin exists
        print("\n👤 Verifying admin user...")
        ensure_admin_exists(pg_conn)
        
        # Verify migration
        verify_migration(pg_conn)
        
        pg_conn.close()
        
        print("\n" + "=" * 60)
        print("✅ MIGRATION COMPLETED SUCCESSFULLY!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Update your .env file to ensure DATABASE_URL is set")
        print("2. The app will now use PostgreSQL automatically")
        print("3. You can optionally backup and remove worklog.db")
        
    except FileNotFoundError as e:
        print(f"\n❌ ERROR: {e}")
    except psycopg.Error as e:
        print(f"\n❌ PostgreSQL Error: {e}")
    except Exception as e:
        print(f"\n❌ Unexpected Error: {e}")

if __name__ == "__main__":
    main()
