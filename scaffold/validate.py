import json
from pathlib import Path

root = Path("/workspace")
contract = json.loads((root / "contracts/request.schema.json").read_text())
samples = json.loads((root / "fixtures/sample-requests.json").read_text())
rules = json.loads((root / "fixtures/rules.json").read_text())
assert contract["type"] == "object"
assert isinstance(samples, list) and samples
assert rules["version"]
print("scaffold inputs valid")
