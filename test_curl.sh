#!/bin/bash
# Curl-based OCR test script for batch processing
# 
# Usage:
#   Single image:  ./test_curl.sh <provider> <image_path>
#   Full folder:   ./test_curl.sh <provider> --folder <folder_path> [parallel_count]
#
# Examples:
#   ./test_curl.sh openrouter /Users/hasanbutt/Downloads/test-pag_0001.png
#   ./test_curl.sh vertex --folder /Users/hasanbutt/Downloads/test-pag_0001/
#   ./test_curl.sh openrouter --folder /Users/hasanbutt/Downloads/test-pag_0001/ 5
#
# Providers:
#   openrouter - Gemini 3 Flash + Gemini 3 Pro (non-GDPR)
#   vertex     - Gemini 2.5 Flash + Pro (GDPR compliant)
#
# API Parameters (can be customized in script):
#   two_step_ocr: true (default) - Use two-step OCR with Pro fallback
#   confidence_threshold: 0.95 (default) - Threshold for Step 2 trigger
#   step1_model: null (uses default: gemini-2.0-flash or gemini-3.0-flash)
#   step2_model: null (uses default: gemini-2.0-pro or gemini-3.0-pro)
#   temperature: 0.0 (default)
#   top_p: 1.0 (default)
#   top_k: 40 (default)

# Configuration
API_URL="https://biqe-ocr-service-650561295384.europe-west4.run.app"
PROVIDER="${1:-openrouter}"
OUTPUT_DIR="test_output_curl_${PROVIDER}"
POLL_INTERVAL=2
POLL_TIMEOUT=300

# Network Settings (increased timeouts for Windows/slow networks)
CURL_CONNECT_TIMEOUT=60   # Connection timeout (seconds)
CURL_MAX_TIME=180         # Max time for upload (seconds)
CURL_RETRIES=3            # Number of retries on network failure

# OCR Parameters (customize these)
TWO_STEP_OCR="true"
CONFIDENCE_THRESHOLD="0.95"
STEP1_MODEL=""  # Empty = use default
STEP2_MODEL=""  # Empty = use default
TEMPERATURE="0.0"
TOP_P="1.0"
TOP_K="40"

# Parallel execution (default: 1 = sequential)
PARALLEL_JOBS="${4:-1}"

# Create output directory
mkdir -p "$OUTPUT_DIR"

echo "============================================"
echo "OCR Test with curl"
echo "============================================"
echo "Provider: $PROVIDER"
echo "Output dir: $OUTPUT_DIR"
echo "Two-step OCR: $TWO_STEP_OCR"
echo "Confidence threshold: $CONFIDENCE_THRESHOLD"
echo "Network: timeout=${CURL_MAX_TIME}s, retries=${CURL_RETRIES}"
echo ""

# Function to format confidence as percentage
format_confidence() {
    local conf="$1"
    # Check if confidence is a decimal (0-1 range)
    if [[ "$conf" == "0."* ]] || [[ "$conf" == "1" ]] || [[ "$conf" == "1.0" ]] || [[ "$conf" == "1.00" ]]; then
        # It's already 1.0 or needs conversion
        local pct
        pct=$(echo "$conf * 100" | bc 2>/dev/null || echo "$conf")
        printf "%.0f" "$pct"
    else
        # Already a percentage
        printf "%.0f" "$conf"
    fi
}

# Function to get file size in human-readable format
get_file_size() {
    local FILE="$1"
    local SIZE_BYTES
    SIZE_BYTES=$(stat -f%z "$FILE" 2>/dev/null || stat -c%s "$FILE" 2>/dev/null || echo "0")
    
    if [ "$SIZE_BYTES" -gt 1048576 ]; then
        echo "$((SIZE_BYTES / 1048576)) MB"
    elif [ "$SIZE_BYTES" -gt 1024 ]; then
        echo "$((SIZE_BYTES / 1024)) KB"
    else
        echo "$SIZE_BYTES bytes"
    fi
}

# Function to submit a single image with retry logic and verbose output
submit_image() {
    local IMAGE_PATH="$1"
    local FILENAME
    FILENAME=$(basename "$IMAGE_PATH")
    
    # Get file size for display
    local FILE_SIZE
    FILE_SIZE=$(get_file_size "$IMAGE_PATH")
    
    # Build form data
    local FORM_DATA=(
        -F "file=@${IMAGE_PATH}"
        -F "filename=${FILENAME}"
        -F "provider=${PROVIDER}"
        -F "two_step_ocr=${TWO_STEP_OCR}"
        -F "confidence_threshold=${CONFIDENCE_THRESHOLD}"
        -F "temperature=${TEMPERATURE}"
        -F "top_p=${TOP_P}"
        -F "top_k=${TOP_K}"
    )
    
    # Add optional model parameters if set
    if [ -n "$STEP1_MODEL" ]; then
        FORM_DATA+=(-F "step1_model=${STEP1_MODEL}")
    fi
    if [ -n "$STEP2_MODEL" ]; then
        FORM_DATA+=(-F "step2_model=${STEP2_MODEL}")
    fi
    
    # Submit with retries and extended timeouts
    local RESPONSE=""
    local RETRY=0
    local CURL_ERROR=""
    local UPLOAD_TIME=""
    local HTTP_CODE=""
    local SPEED_UPLOAD=""
    
    while [ $RETRY -lt $CURL_RETRIES ]; do
        # Create temp file for curl write-out stats
        local START_TIME
        START_TIME=$(date +%s.%N 2>/dev/null || date +%s)
        
        # Use extended timeouts with write-out for stats
        # -w outputs: http_code, time_total, speed_upload
        local CURL_OUTPUT
        CURL_OUTPUT=$(curl -s \
            --connect-timeout $CURL_CONNECT_TIMEOUT \
            --max-time $CURL_MAX_TIME \
            -w "\n__STATS__%{http_code}|%{time_total}|%{speed_upload}" \
            -X POST "$API_URL/gcs-batch/process-image" \
            "${FORM_DATA[@]}" 2>&1)
        CURL_ERROR=$?
        
        # Split response and stats
        RESPONSE=$(echo "$CURL_OUTPUT" | sed 's/__STATS__.*//')
        local STATS
        STATS=$(echo "$CURL_OUTPUT" | grep "__STATS__" | sed 's/__STATS__//')
        
        if [ -n "$STATS" ]; then
            HTTP_CODE=$(echo "$STATS" | cut -d'|' -f1)
            UPLOAD_TIME=$(echo "$STATS" | cut -d'|' -f2)
            SPEED_UPLOAD=$(echo "$STATS" | cut -d'|' -f3)
            
            # Convert speed to KB/s or MB/s
            if [ -n "$SPEED_UPLOAD" ]; then
                local SPEED_KB
                SPEED_KB=$(echo "$SPEED_UPLOAD / 1024" | bc 2>/dev/null || echo "?")
                if [ "$SPEED_KB" != "?" ] && [ "$SPEED_KB" -gt 1024 ]; then
                    SPEED_UPLOAD="$((SPEED_KB / 1024)) MB/s"
                else
                    SPEED_UPLOAD="${SPEED_KB} KB/s"
                fi
            fi
        fi
        
        # Check if curl succeeded
        if [ $CURL_ERROR -eq 0 ] && [ -n "$RESPONSE" ]; then
            break
        fi
        
        RETRY=$((RETRY + 1))
        if [ $RETRY -lt $CURL_RETRIES ]; then
            echo "    ⚠️  Retry $RETRY/$CURL_RETRIES: network issue (code $CURL_ERROR)" >&2
            sleep 2
        fi
    done
    
    # Parse response
    local JOB_ID
    JOB_ID=$(echo "$RESPONSE" | jq -r '.job_id' 2>/dev/null)
    
    if [ "$JOB_ID" == "null" ] || [ -z "$JOB_ID" ]; then
        # Include more helpful error info
        if [ $CURL_ERROR -ne 0 ]; then
            echo "ERROR:$FILENAME:Network error (curl code $CURL_ERROR) - check firewall/antivirus"
        elif [ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "200" ]; then
            echo "ERROR:$FILENAME:HTTP $HTTP_CODE - server error"
        elif [ -z "$RESPONSE" ]; then
            echo "ERROR:$FILENAME:Empty response - connection blocked or timeout"
        else
            echo "ERROR:$FILENAME:$RESPONSE"
        fi
    else
        # Output with stats: JOB_ID:FILENAME:SIZE:TIME:SPEED
        echo "$JOB_ID:$FILENAME:$FILE_SIZE:${UPLOAD_TIME}s:$SPEED_UPLOAD"
    fi
}

# Function to poll for a single job result
poll_job() {
    local JOB_ID="$1"
    local FILENAME="$2"
    local BASENAME="${FILENAME%.*}"
    local START_TIME
    START_TIME=$(date +%s)
    
    while true; do
        local CURRENT_TIME
        CURRENT_TIME=$(date +%s)
        local ELAPSED=$((CURRENT_TIME - START_TIME))
        
        if [ $ELAPSED -gt $POLL_TIMEOUT ]; then
            echo "  $FILENAME: TIMEOUT after ${POLL_TIMEOUT}s"
            return 1
        fi
        
        local STATUS_RESPONSE
        STATUS_RESPONSE=$(curl -s "$API_URL/gcs-batch/jobs/$JOB_ID/status")
        local STATUS
        STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status')
        
        if [ "$STATUS" == "completed" ]; then
            # Extract result
            local TEXT
            TEXT=$(echo "$STATUS_RESPONSE" | jq -r '.text_content')
            local CONFIDENCE
            CONFIDENCE=$(echo "$STATUS_RESPONSE" | jq -r '.confidence')
            local MODEL
            MODEL=$(echo "$STATUS_RESPONSE" | jq -r '.model_used')
            local CHARS=${#TEXT}
            local CONF_PCT
            CONF_PCT=$(format_confidence "$CONFIDENCE")
            
            echo "  $FILENAME: ${CONF_PCT}% (${MODEL}), ${CHARS} chars, ${ELAPSED}s"
            
            # Save text to file
            echo "$TEXT" > "$OUTPUT_DIR/${BASENAME}.txt"
            
            # Save full JSON
            echo "$STATUS_RESPONSE" | jq '.' > "$OUTPUT_DIR/${BASENAME}.json"
            
            return 0
            
        elif [ "$STATUS" == "failed" ]; then
            local ERROR
            ERROR=$(echo "$STATUS_RESPONSE" | jq -r '.error')
            echo "  $FILENAME: FAILED - $ERROR"
            return 1
        fi
        
        sleep $POLL_INTERVAL
    done
}

# Function to process a single image sequentially
process_image() {
    local IMAGE_PATH="$1"
    local FILENAME
    FILENAME=$(basename "$IMAGE_PATH")
    local BASENAME="${FILENAME%.*}"
    
    echo "Processing: $FILENAME"
    
    # Check if image exists
    if [ ! -f "$IMAGE_PATH" ]; then
        echo "  Error: File not found"
        return 1
    fi
    
    # Check if already processed
    if [ -f "$OUTPUT_DIR/${BASENAME}.txt" ]; then
        echo "  Already processed, skipping"
        return 0
    fi
    
    # Submit
    local RESULT
    RESULT=$(submit_image "$IMAGE_PATH")
    
    if [[ "$RESULT" == ERROR:* ]]; then
        echo "  Submit failed: $RESULT"
        return 1
    fi
    
    local JOB_ID
    JOB_ID=$(echo "$RESULT" | cut -d: -f1)
    echo "  Job: $JOB_ID"
    
    # Poll
    poll_job "$JOB_ID" "$FILENAME"
}

# Function to process images in parallel batches
process_parallel() {
    local FOLDER_PATH="$1"
    local BATCH_SIZE="${2:-5}"
    
    # Find all image files
    local IMAGES
    IMAGES=$(find "$FOLDER_PATH" -maxdepth 1 -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.webp" \) | sort)
    
    # Count images
    local IMAGE_COUNT
    IMAGE_COUNT=$(echo "$IMAGES" | grep -c . || echo 0)
    echo "Found $IMAGE_COUNT images"
    echo "Processing in batches of $BATCH_SIZE"
    echo ""
    
    local SUCCESS=0
    local FAILED=0
    local BATCH_NUM=0
    local JOBS=()
    
    while IFS= read -r IMAGE; do
        if [ -z "$IMAGE" ] || [ ! -f "$IMAGE" ]; then
            continue
        fi
        
        local FILENAME
        FILENAME=$(basename "$IMAGE")
        local BASENAME="${FILENAME%.*}"
        
        # Skip if already processed
        if [ -f "$OUTPUT_DIR/${BASENAME}.txt" ]; then
            echo "Skip: $FILENAME (already processed)"
            SUCCESS=$((SUCCESS + 1))
            continue
        fi
        
        # Submit job with upload stats
        local RESULT
        RESULT=$(submit_image "$IMAGE")
        
        if [[ "$RESULT" == ERROR:* ]]; then
            local ERR_MSG
            ERR_MSG=$(echo "$RESULT" | cut -d: -f3-)
            echo "Submit: $FILENAME"
            echo "  ❌ Failed: $ERR_MSG"
            FAILED=$((FAILED + 1))
            continue
        fi
        
        # Parse result: JOB_ID:FILENAME:SIZE:TIME:SPEED
        local JOB_ID FILE_SIZE UPLOAD_TIME UPLOAD_SPEED
        JOB_ID=$(echo "$RESULT" | cut -d: -f1)
        FILE_SIZE=$(echo "$RESULT" | cut -d: -f3)
        UPLOAD_TIME=$(echo "$RESULT" | cut -d: -f4)
        UPLOAD_SPEED=$(echo "$RESULT" | cut -d: -f5)
        
        echo "Submit: $FILENAME ($FILE_SIZE) → uploaded in ${UPLOAD_TIME} @ ${UPLOAD_SPEED}"
        echo "  Job: $JOB_ID"
        
        JOBS+=("$JOB_ID:$FILENAME")
        
        # Process batch when full
        if [ ${#JOBS[@]} -ge $BATCH_SIZE ]; then
            BATCH_NUM=$((BATCH_NUM + 1))
            echo ""
            echo "--- Polling Batch $BATCH_NUM (${#JOBS[@]} jobs) ---"
            
            for JOB_INFO in "${JOBS[@]}"; do
                local JID
                JID=$(echo "$JOB_INFO" | cut -d: -f1)
                local FNAME
                FNAME=$(echo "$JOB_INFO" | cut -d: -f2)
                
                if poll_job "$JID" "$FNAME"; then
                    SUCCESS=$((SUCCESS + 1))
                else
                    FAILED=$((FAILED + 1))
                fi
            done
            
            JOBS=()
            echo ""
        fi
        
    done <<< "$IMAGES"
    
    # Process remaining jobs
    if [ ${#JOBS[@]} -gt 0 ]; then
        BATCH_NUM=$((BATCH_NUM + 1))
        echo ""
        echo "--- Polling Final Batch $BATCH_NUM (${#JOBS[@]} jobs) ---"
        
        for JOB_INFO in "${JOBS[@]}"; do
            local JID
            JID=$(echo "$JOB_INFO" | cut -d: -f1)
            local FNAME
            FNAME=$(echo "$JOB_INFO" | cut -d: -f2)
            
            if poll_job "$JID" "$FNAME"; then
                SUCCESS=$((SUCCESS + 1))
            else
                FAILED=$((FAILED + 1))
            fi
        done
    fi
    
    echo ""
    echo "============================================"
    echo "SUMMARY"
    echo "============================================"
    echo "Success: $SUCCESS"
    echo "Failed:  $FAILED"
    echo "Total:   $IMAGE_COUNT"
    echo "Output:  $OUTPUT_DIR/"
}

# Main logic
if [ "$2" == "--folder" ]; then
    # Batch mode
    FOLDER_PATH="${3:-.}"
    PARALLEL="${4:-5}"  # Default: 5 parallel
    
    echo "Folder: $FOLDER_PATH"
    echo ""
    
    process_parallel "$FOLDER_PATH" "$PARALLEL"
    
else
    # Single image mode
    IMAGE_PATH="$2"
    
    if [ -z "$IMAGE_PATH" ]; then
        echo "Usage:"
        echo "  Single image:  ./test_curl.sh <provider> <image_path>"
        echo "  Full folder:   ./test_curl.sh <provider> --folder <folder_path> [parallel_count]"
        echo ""
        echo "Providers:"
        echo "  openrouter - Gemini 3 Flash + Gemini 3 Pro"
        echo "  vertex     - Gemini 2.5 Flash + Pro (GDPR)"
        echo ""
        echo "Examples:"
        echo "  ./test_curl.sh openrouter /path/to/image.png"
        echo "  ./test_curl.sh vertex --folder /path/to/images/ 10"
        echo ""
        echo "API Parameters (edit script to customize):"
        echo "  TWO_STEP_OCR=$TWO_STEP_OCR"
        echo "  CONFIDENCE_THRESHOLD=$CONFIDENCE_THRESHOLD"
        echo "  TEMPERATURE=$TEMPERATURE"
        echo "  TOP_P=$TOP_P"
        echo "  TOP_K=$TOP_K"
        exit 1
    fi
    
    echo "Image: $IMAGE_PATH"
    echo ""
    
    process_image "$IMAGE_PATH"
fi

echo ""
echo "Done!"
