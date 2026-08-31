#!/usr/bin/env bash
set -euo pipefail

# Manual API testing script for JumpTo

API_BASE="http://localhost:8000"

echo "=== JumpTo API Manual Tests ==="
echo

# Test 1: Health check
echo "1. Health check:"
curl -s "$API_BASE/health" | jq .
echo

# Test 2: Invalid URL
echo "2. Invalid YouTube URL (should return 400):"
curl -s -X POST "$API_BASE/api/search" \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://example.com/video", "keyword": "test"}' | jq .
echo

# Test 3: Empty keyword
echo "3. Empty keyword (should return 422):"
curl -s -X POST "$API_BASE/api/search" \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "keyword": ""}' | jq .
echo

# Test 4: Valid URL - cache miss (will create job)
echo "4. Valid YouTube URL (cache miss - returns job_id):"
curl -s -X POST "$API_BASE/api/search" \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "keyword": "never"}' | jq .
echo

# Test 5: Check job status (replace JOB_ID from above)
# JOB_ID="..."
# echo "5. Job status:"
# curl -s "$API_BASE/api/status/$JOB_ID" | jq .

# Test 6: CORS preflight
echo "6. CORS preflight (allowed origin):"
curl -s -X OPTIONS "$API_BASE/api/search" \
  -H "Origin: http://localhost:5173" \
  -H "Access-Control-Request-Method: POST" \
  -D - | head -20
echo

# Test 7: CORS preflight (disallowed origin)
echo "7. CORS preflight (disallowed origin):"
curl -s -X OPTIONS "$API_BASE/api/search" \
  -H "Origin: http://evil.com" \
  -H "Access-Control-Request-Method: POST" \
  -D - | head -20
echo

echo "=== Manual tests complete ==="
echo "Note: For full testing, run: cd backend && pytest --cov=app --cov-fail-under=80 -v"