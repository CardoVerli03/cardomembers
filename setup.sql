-- ============================================================
-- TELEGRAM MEMBERSHIP BOT - DATABASE SETUP
-- ============================================================
-- Run this SQL in your Supabase SQL Editor:
-- https://supabase.com/dashboard → your project → SQL Editor → New Query
-- Paste this entire file and click RUN
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    upgraded BOOLEAN NOT NULL DEFAULT FALSE,
    reminded BOOLEAN NOT NULL DEFAULT FALSE
);

-- Index for faster expiry queries
CREATE INDEX IF NOT EXISTS idx_users_status_expires ON users(status, expires_at);

-- Index for reminder queries
CREATE INDEX IF NOT EXISTS idx_users_reminded ON users(status, reminded, expires_at);

-- Enable Row Level Security (optional but recommended)
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

-- Allow public read/write (since we use anon key)
CREATE POLICY "Allow all operations" ON users
    FOR ALL
    USING (true)
    WITH CHECK (true);
