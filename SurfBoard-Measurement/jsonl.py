import json
import csv
import random

# Configuration
CSV_FILE = 'surf_data.csv'
CLOUD_BUCKET = 'gs://tomer-surf-training-data' # ⚠️ MAKE SURE THIS MATCHES YOUR BUCKET EXACTLY
TRAIN_FILE = 'training_data.jsonl'
TEST_FILE = 'validation_data.jsonl'

# Read the data from your spreadsheet
dataset = []
with open(CSV_FILE, mode='r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # Safety check: skip any empty rows at the bottom of the CSV
        if row.get('video_filename') and row.get('video_filename').strip() != '':
            dataset.append(row)

# Verify we have exactly 100 videos before proceeding
if len(dataset) != 100:
    print(f"⚠️ Warning: Found {len(dataset)} videos in the CSV, expected 100.")

# Shuffle the data randomly
random.shuffle(dataset)

# Calculate the 80/20 split
split_index = int(len(dataset) * 0.8)
train_data = dataset[:split_index]
test_data = dataset[split_index:]

def write_jsonl(data_list, output_filename):
    with open(output_filename, 'w', encoding='utf-8') as f:
        for data in data_list:
            
            prompt_text = f"The surfer claims their skill level is '{data['skill']}'. They weigh {data['weight']}kg and are {data['height']}cm tall. Analyze the video and provide the ideal surfboard dimensions."
            
            # Added a try/except block just in case a CSV cell has weird formatting (like "6'2" instead of just "6")
            try:
                ideal_output = json.dumps({
                    "is_surfing": True,
                    "skill_assessment_text": data["notes"],
                    "rec_liters": float(data["liters"]),
                    "rec_feet": int(data["feet"]),
                    "rec_inches": float(data["inches"])
                })
            except ValueError as e:
                print(f"❌ Skipping {data['video_filename']} due to number formatting error: {e}")
                continue
            
            jsonl_line = {
                "systemInstruction": {
                    "role": "system",
                    "parts": [{"text": "You are a master surfboard shaper and expert surf coach."}]
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"fileData": {"fileUri": f"{CLOUD_BUCKET}/{data['video_filename']}", "mimeType": "video/mp4"}},
                            {"text": prompt_text}
                        ]
                    },
                    {
                        "role": "model",
                        "parts": [{"text": ideal_output}]
                    }
                ]
            }
            
            f.write(json.dumps(jsonl_line) + "\n")

# Generate the files
write_jsonl(train_data, TRAIN_FILE)
write_jsonl(test_data, TEST_FILE)

print(f"✅ Successfully created {TRAIN_FILE} ({len(train_data)} videos)")
print(f"✅ Successfully created {TEST_FILE} ({len(test_data)} videos)")