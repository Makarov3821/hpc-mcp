import json,sys
from pathlib import Path
request=json.load(sys.stdin)
print(json.dumps({"script": (Path(__file__).parent/"original"/"job.sh").read_text(), "input_files":["input.txt"],"outputs":["result.txt"]}))
