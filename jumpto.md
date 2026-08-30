# YouTube Keyword Timestamp Finder - Complete Architecture Specification

## 📋 Project Overview

**Project Name:** YouTube Keyword Timestamp Finder  
**Goal:** Web application where users input YouTube URL + keyword, receive exact timestamps when word is spoken.

## 🎯 Requirements

- User inputs: YouTube URL + keyword (single word or phrase)
- Exact phrase matching only (no fuzzy match)
- Output: Timestamps + video progress seconds
- Cache transcripts (avoid re-processing)
- Scale to 100+ concurrent users
- Hybrid transcription (free tier + paid upgrade)
- Fast search (<20ms using PostgreSQL GIN indexes)
- Web app only (React frontend)
- YouTube videos only

## 🏗️ Architecture

Frontend (React) → Backend (FastAPI) → PostgreSQL + Redis + Assembly.ai

**Foreground (fast):**
- POST /api/search → validate + queue transcription job
- GET /api/status/{job_id} → check progress
- GET /api/video/{video_id}/search → return cached results

**Background (Celery):**
- download_youtube_audio() → yt-dlp [30s-5min]
- transcribe_with_assembly() → Assembly.ai API [30s-10min]
- parse_and_store_transcript() → PostgreSQL [10-30s]

## 🛠️ Tech Stack

- Backend: FastAPI (Python)
- Database: PostgreSQL (with GIN indexes for full-text search)
- Task Queue: Celery + Redis
- Transcription: Assembly.ai API
- Audio: yt-dlp
- Frontend: React + Axios

## 🗄️ Database Schema

### videos table
```sql
CREATE TABLE videos (
    id UUID PRIMARY KEY,
    youtube_url VARCHAR UNIQUE,
    video_id VARCHAR UNIQUE,
    title VARCHAR,
    duration_seconds INT,
    transcript TEXT,
    transcript_tsvector TSVECTOR,
    transcribed_at TIMESTAMP,
    created_at TIMESTAMP
);
CREATE INDEX idx_transcript_fts ON videos USING GIN(transcript_tsvector);
```

### transcript_words table
```sql
CREATE TABLE transcript_words (
    id UUID PRIMARY KEY,
    video_id UUID REFERENCES videos(id),
    word_index INT,
    word VARCHAR,
    start_time FLOAT,
    end_time FLOAT
);
CREATE INDEX idx_video_word ON transcript_words(video_id, word);
```

## 📡 API Endpoints

### POST /api/search
Request: { "youtube_url": "...", "keyword": "love" }
Response (cached): { "status": "found", "results": [{"timestamp": "00:15", "progress_seconds": 15}] }
Response (not cached): { "status": "processing", "job_id": "abc123" }

### GET /api/status/{job_id}
Returns: { "status": "pending|processing|completed|failed", ... }

### GET /api/video/{video_id}/search?keyword=love
Returns: { "status": "found", "results": [...] }

## ⚙️ Background Tasks (Celery)

```python
@celery_app.task(bind=True, max_retries=3)
def download_and_transcribe(self, youtube_url, video_id):
    # 1. Download audio (yt-dlp)
    # 2. Transcribe (Assembly.ai)
    # 3. Store in PostgreSQL with indexes
    # 4. Return results
```

Retries: 3 with exponential backoff
Timeout: 30 min max

## 🔑 Environment Variables

DATABASE_URL=postgresql://user:password@localhost/youtube_keyword_finder
ASSEMBLY_API_KEY=99e8ad3adb524cf7a6ba98c2778f55a5[text](../../nodeJs/aniq/AGENTS.md)
REDIS_URL=redis://localhost:6379
ENVIRONMENT=development

## 📁 Project Structure